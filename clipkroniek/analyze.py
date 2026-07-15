#!/usr/bin/env python3
"""
Clipkroniek — performance analyzer + light strategist.

Runs daily on GitHub Actions:
  1. Tracks followers (followers_count field — works at any size) -> followers.json
  2. Pulls per-Reel Insights (reach, shares, saved, views, watch time, follows)
     into history.json
  3. Prints the A/B readout: reach + conversion by REGION (western vs asian) and
     by GAME (gta vs valorant) and by region|game.
  4. Weekly (or --strategize) refreshes strategy.json's `learnings` via Claude so
     the caption/selection guidance reflects what's actually converting.

The slot GRID (which region/game on which day) is left to human/strategic tuning
for now — the A/B readout is what informs that call.

Env: ANTHROPIC_API_KEY, IG_ACCESS_TOKEN  (IG_USER_ID optional).
Flags: --strategize (force learnings refresh), --no-strategize (metrics only),
       DRY_RUN=1 (measure + print, write nothing).
"""
import os
import sys
import json
import datetime
import requests

import twitch
import newsscan
import youtube
from clippost import (
    BRAND_NAME, CLAUDE_MODEL, STRATEGY_FILE,
    env, http, now_utc, load_strategy, load_history, save_history,
    resolve_ig_user_id, _graph_node,
)

FOLLOWERS_FILE = "followers.json"
REVIEW_EVERY_DAYS = 7
REMEASURE_WITHIN_DAYS = 14
YT_REMEASURE_DAYS = 14
MIN_AGE_HOURS_TO_MEASURE = 18

# Reel-valid insight metrics that also work below 100 followers.
# profile_visits splits the funnel: reach -> profile_visit -> follow (#5).
MEDIA_METRICS = ["reach", "likes", "comments", "saved", "shares",
                 "total_interactions", "views", "ig_reels_avg_watch_time",
                 "profile_visits", "follows"]

RECENT_WINDOW_DAYS = 28   # scoring/adaptation ignores posts older than this (#18)


def _post_age_hours(post):
    try:
        return (now_utc() - datetime.datetime.fromisoformat(post["date_utc"])).total_seconds() / 3600.0
    except Exception:
        return 1e9


def _recent_posts(history):
    cutoff = RECENT_WINDOW_DAYS * 24
    return [p for p in history.get("posts", []) if _post_age_hours(p) <= cutoff]


def _retention(post):
    """avg watch time / source clip duration, clamped for loops. None if unknown
    (older posts before duration_s was recorded)."""
    m = post.get("metrics") or {}
    wt = m.get("ig_reels_avg_watch_time")
    dur = post.get("duration_s")
    try:
        if wt and dur and float(dur) > 0:
            return min(1.5, (float(wt) / 1000.0) / float(dur))
    except Exception:
        pass
    return None


def _load(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def fetch_account(ig_id, token):
    r = http("GET", _graph_node(ig_id),
             params={"fields": "followers_count,follows_count,media_count,username",
                     "access_token": token}, timeout=60)
    r.raise_for_status()
    return r.json()


def record_followers(account):
    data = _load(FOLLOWERS_FILE, [])
    today = now_utc().date().isoformat()
    data = [e for e in data if e.get("date") != today]
    data.append({"date": today, "followers": account.get("followers_count"),
                 "media_count": account.get("media_count")})
    data.sort(key=lambda e: e["date"])
    if os.environ.get("DRY_RUN") != "1":
        with open(FOLLOWERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    return data


def fetch_media_insights(media_id, token):
    def _req(metrics):
        r = http("GET", f"{_graph_node(media_id)}/insights",
                 params={"metric": ",".join(metrics), "access_token": token}, timeout=60)
        r.raise_for_status()
        out = {}
        for item in r.json().get("data", []):
            vals = item.get("values") or [{}]
            v = vals[0].get("value")
            if v is None:
                v = (item.get("total_value") or {}).get("value")
            out[item.get("name")] = v
        return out
    try:
        return _req(MEDIA_METRICS)
    except requests.HTTPError:
        out = {}
        for m in MEDIA_METRICS:
            try:
                out.update(_req([m]))
            except requests.HTTPError:
                continue
        return out


def measure_posts(history, token):
    now = now_utc()
    measured = 0
    for post in history.get("posts", []):
        mid = post.get("media_id")
        if not mid:
            continue
        try:
            posted = datetime.datetime.fromisoformat(post["date_utc"])
        except Exception:
            continue
        age_h = (now - posted).total_seconds() / 3600.0
        if age_h < MIN_AGE_HOURS_TO_MEASURE:
            continue
        if age_h > REMEASURE_WITHIN_DAYS * 24 and post.get("metrics"):
            continue
        try:
            m = fetch_media_insights(mid, token)
        except requests.HTTPError as e:
            print(f"  insights failed for {mid}: {e}")
            continue
        post["metrics"] = m
        post["measured_at"] = now.isoformat()
        # Freeze a fixed-age snapshot so the A/B compares like-with-like instead of
        # yesterday's fresh post against fully-matured ones (#18).
        if 18 <= age_h <= 36 and not post.get("metrics_24h"):
            post["metrics_24h"] = dict(m)
        measured += 1
        print(f"  {post.get('game')}/{post.get('region')}: reach={m.get('reach')} "
              f"shares={m.get('shares')} saved={m.get('saved')} follows={m.get('follows')}")
    return measured


def measure_youtube(history):
    """Pull YouTube performance for cross-posted videos (daily reels, Top-3, long-form)
    into each record's `youtube.metrics`, so we can eventually compare what works on
    YouTube vs Instagram. Gated on the YT secrets AND a token with read scopes — until
    the owner re-mints with youtube.readonly + yt-analytics.readonly, this logs the
    fix and no-ops (safe to ship now). Private videos read ~0 (expected until public)."""
    if not youtube.configured():
        return 0
    try:
        token = youtube.get_access_token()
    except Exception as e:
        print(f"  youtube: no access token ({str(e)[:120]}) — skipping YT measurement.")
        return 0

    now = now_utc()
    # Self-verifying scope probe (also logs a channel growth snapshot every run).
    try:
        ch = youtube.get_channel(token)
        if ch:
            print(f"  YouTube channel: {ch.get('title')} — {ch.get('subscribers')} subs, "
                  f"{ch.get('views')} views, {ch.get('videos')} videos")
    except youtube.ScopeError:
        print("  youtube: token lacks read scope — re-mint YT_REFRESH_TOKEN with "
              "youtube.readonly + yt-analytics.readonly (see clipkroniek/CLAUDE.md) to "
              "turn on YouTube measurement.")
        return 0
    except Exception as e:
        print(f"  youtube channel probe failed: {e}")
    try:
        youtube.channel_analytics(token, "2005-04-23", now.date().isoformat())
        print("  youtube: analytics scope OK (watch-time / retention available).")
    except youtube.ScopeError:
        print("  youtube: analytics scope (yt-analytics.readonly) missing — basic stats "
              "only until you re-mint with it.")
    except Exception as e:
        print(f"  youtube analytics probe failed: {e}")

    targets = []
    for p in history.get("posts", []):
        vid = (p.get("youtube") or {}).get("id")
        if not vid:
            continue
        age_h = _post_age_hours(p)
        if age_h < MIN_AGE_HOURS_TO_MEASURE:
            continue
        if age_h > YT_REMEASURE_DAYS * 24 and (p.get("youtube") or {}).get("metrics"):
            continue
        targets.append((p, vid))
    if not targets:
        return 0

    try:
        stats = youtube.get_video_stats(token, [v for _, v in targets])
    except youtube.ScopeError:
        print("  youtube: token lacks read scope — re-mint YT_REFRESH_TOKEN with "
              "youtube.readonly + yt-analytics.readonly (see clipkroniek/CLAUDE.md) "
              "to turn on YouTube measurement.")
        return 0
    except Exception as e:
        print(f"  youtube stats failed: {e}")
        return 0

    start, end = "2005-04-23", now.date().isoformat()   # YT launch -> today (lifetime)
    analytics_ok = True
    measured = 0
    for p, vid in targets:
        m = dict(stats.get(vid, {}))
        if analytics_ok:
            try:
                a = youtube.get_video_analytics(token, vid, start, end)
                for k in ("estimatedMinutesWatched", "averageViewDuration",
                          "averageViewPercentage", "subscribersGained"):
                    if k in a:
                        m[k] = a[k]
            except youtube.ScopeError:
                analytics_ok = False
                print("  youtube: analytics scope (yt-analytics.readonly) missing — "
                      "storing basic stats only; re-mint to add watch-time/retention.")
            except Exception as e:
                print(f"  youtube analytics failed for {vid}: {e}")
        yt = dict(p.get("youtube") or {})
        yt["metrics"] = m
        yt["measured_at"] = now.isoformat()
        p["youtube"] = yt
        measured += 1
        print(f"  YT {p.get('game')}/{p.get('format') or 'single'}: views={m.get('views')} "
              f"watch%={m.get('averageViewPercentage')} subs+={m.get('subscribersGained')}")
    return measured


def youtube_readout(history):
    """Lightweight YouTube aggregate by game/format over the recent window — the seed
    of the eventual IG-vs-YT comparison. Empty until YT videos have real (public) data."""
    groups = {}
    for p in _recent_posts(history):
        m = (p.get("youtube") or {}).get("metrics") or {}
        if not (m.get("views") or 0):
            continue
        for k in (f"yt:game:{p.get('game')}", f"yt:format:{p.get('format') or 'single'}"):
            groups.setdefault(k, []).append(m)

    def agg(ms):
        n = len(ms)
        return {"videos": n,
                "avg_views": round(sum(x.get("views") or 0 for x in ms) / n, 1),
                "avg_watch_pct": round(sum(float(x.get("averageViewPercentage") or 0)
                                           for x in ms) / n, 1),
                "subs_gained": sum(int(x.get("subscribersGained") or 0) for x in ms)}
    return {k: agg(v) for k, v in groups.items()}


def _ratio(n, d):
    return round(n / d, 4) if d else None


def ab_readout(history):
    groups = {}
    for p in _recent_posts(history):
        if not ((p.get("metrics") or {}).get("reach") or 0):
            continue
        src = p.get("source", "twitch")
        cur = "curated" if p.get("curated") else "general"
        fmt = p.get("format") or "single"
        keys = [f"region:{p.get('region')}", f"game:{p.get('game')}",
                f"source:{src}", f"curation:{cur}", f"format:{fmt}",
                f"hour:{p.get('slot_hour')}",
                f"{src}|{p.get('game')}", f"{p.get('region')}|{p.get('game')}"]
        if p.get("trimmed") is True:
            keys.append("trim:on")
        elif p.get("trimmed") is False:
            keys.append("trim:off")
        for k in keys:
            groups.setdefault(k, []).append(p)

    def agg(posts):
        n = len(posts)
        R = sum((p.get("metrics") or {}).get("reach") or 0 for p in posts)

        def s(metric):
            return sum((p.get("metrics") or {}).get(metric) or 0 for p in posts)
        rets = [r for r in (_retention(p) for p in posts) if r is not None]
        return {"posts": n, "avg_reach": round(R / n, 1) if n else 0,
                "shares_per_reach": _ratio(s("shares"), R),
                "saves_per_reach": _ratio(s("saved"), R),
                "follows_per_reach": _ratio(s("follows"), R),
                "visits_per_reach": _ratio(s("profile_visits"), R),
                "follows_per_visit": _ratio(s("follows"), s("profile_visits")),
                "views_per_reach": _ratio(s("views"), R),
                "avg_retention": round(sum(rets) / len(rets), 3) if rets else None,
                "total_follows": s("follows")}
    return {k: agg(v) for k, v in groups.items()}


def _due(strategy):
    nxt = strategy.get("next_review")
    if not nxt:
        return True
    try:
        return now_utc().date() >= datetime.date.fromisoformat(nxt)
    except Exception:
        return True


def call_strategist(strategy, readout, followers):
    cur = followers[-1].get("followers") if followers else None
    system = (
        "You are the growth strategist for the Instagram gaming-clip page "
        "'Clipkroniek' (reposts trending game clips WITH creator credit). Goal: reach "
        "that converts to FOLLOWS. You get an A/B readout keyed by region "
        "(western=English clips, asian=Asian-language clips), game, source, format, "
        "hour, curation, and trim (smart-trim on/off). How to read it:\n"
        "- REGION is the primary experiment. IGNORE any cell with posts < 8 — it is "
        "noise; draw no conclusions from it.\n"
        "- The curation cell (curated vs general) is CONFOUNDED (curated clips exist "
        "only on western slots) — don't over-read it.\n"
        "- Funnel: reach -> profile_visit -> follow. Use visits_per_reach (are people "
        "tapping through?) vs follows_per_visit (do they follow once they do?) to "
        "locate WHERE conversion leaks. avg_retention (watch-through) is the best "
        "quality signal while follows are still near zero.\n"
        "- Rank options by follows_per_reach, then shares_per_reach, then retention.\n\n"
        "Write a 'learnings' paragraph (UNDER 150 words) that the CAPTION WRITER and "
        "clip selector read before EVERY post — make it ACTIONABLE for them: which "
        "hook/caption styles and search keywords convert, what region/game/hour/format "
        "to favour or drop, and whether to keep or flip the smart-trim test. If data "
        "is thin (most cells < 8 posts) say so and advise holding. Return ONLY the "
        "learnings text — no JSON, no preamble."
    )
    payload = {"current_followers": cur, "ab_readout": readout,
               "active_games": list(strategy.get("games", {})),
               "current_slots": strategy.get("slots"),
               "current_learnings": strategy.get("learnings")}
    body = {"model": CLAUDE_MODEL, "max_tokens": 700, "system": system,
            "messages": [{"role": "user",
                          "content": "Readout:\n" + json.dumps(payload, ensure_ascii=False, indent=2)}]}
    resp = http("POST", "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": env("ANTHROPIC_API_KEY"),
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json=body, timeout=90)
    resp.raise_for_status()
    return "".join(b.get("text", "") for b in resp.json().get("content", [])
                   if b.get("type") == "text").strip()


# =============================================================================
# Adaptive game rotation
# =============================================================================
def _game_score(posts):
    """Score one game's measured posts (list of post dicts). Goal is FOLLOWS;
    shares are the strongest reach signal; RETENTION is the best quality signal at
    tiny follower counts (where follows/shares are ~0 and the score would otherwise
    degenerate to the avg-reach tiebreak that already failed); avg reach breaks
    ties. Returns (score, n_measured)."""
    n = len(posts)
    R = sum((p.get("metrics") or {}).get("reach") or 0 for p in posts)
    if not n or not R:
        return 0.0, n
    fo = sum((p.get("metrics") or {}).get("follows") or 0 for p in posts)
    sh = sum((p.get("metrics") or {}).get("shares") or 0 for p in posts)
    rets = [r for r in (_retention(p) for p in posts) if r is not None]
    ret = (sum(rets) / len(rets)) if rets else 0.0
    score = (fo / R) * 10000 + (sh / R) * 1000 + ret * 100 + (R / n) * 0.001
    return score, n


def _active_games(strategy):
    return sorted({s.get("game") for s in strategy.get("slots", []) if s.get("game")})


def _most_slotted(strategy, games):
    counts = {}
    for s in strategy.get("slots", []):
        g = s.get("game")
        if g in games:
            counts[g] = counts.get(g, 0) + 1
    return max(counts, key=counts.get) if counts else (games[0] if games else None)


def _news_candidates(strategy, active):
    """News-scan candidates, Twitch-validated, not already active."""
    registry = strategy.get("games", {})
    try:
        key = env("ANTHROPIC_API_KEY")
    except SystemExit:
        return []
    benched = [registry[g] for g in registry if g not in active]
    raw = newsscan.scan_games(key, http, [registry[g] for g in active], benched)

    cid = os.environ.get("TWITCH_CLIENT_ID")
    secret = os.environ.get("TWITCH_CLIENT_SECRET")
    tok = [None]

    def canonical(name):
        """Twitch's canonical category name, or None if it doesn't exist. Without
        Twitch creds, trust the scanner's name. Never raises."""
        if not (cid and secret):
            return name
        try:
            if tok[0] is None:
                tok[0] = twitch.get_app_token(cid, secret, http)
            found = twitch.find_game(name, cid, tok[0], http)
            return found[1] if found else None
        except Exception:
            return None

    active_names = {(registry.get(g) or "").strip().lower() for g in active}
    out, seen = [], set(active)
    for c in raw:
        if not isinstance(c, dict):
            continue
        k = (c.get("key") or "").strip().lower().replace(" ", "_")
        name = (c.get("twitch_name") or "").strip()
        if not k or not name or k in seen:
            continue
        canon = canonical(name)
        if not canon:
            print(f"    (candidate {k} '{name}' not on Twitch; skipping)")
            continue
        if canon.strip().lower() in active_names:
            continue                          # already active under another key
        c["key"], c["twitch_name"] = k, canon
        out.append(c)
        seen.add(k)
    return out


def rotate_games(strategy, history, dry):
    """Weekly: swap the worst-performing active game's slots for the best news
    candidate (launch-wave releases jump the queue). One swap per cycle. Mutates
    `strategy` in place; returns True if it changed anything."""
    cfg = strategy.get("game_rotation", {}) or {}
    if not cfg.get("enabled", True):
        print("  [rotation] disabled in strategy.json")
        return False
    min_active = int(cfg.get("min_active", 2))
    min_posts = int(cfg.get("min_posts_to_judge", 4))
    add_mom = int(cfg.get("add_momentum", 65))
    max_active = int(cfg.get("max_active", 3))

    registry = dict(strategy.get("games", {}))
    active = _active_games(strategy)

    rows = {}
    for p in _recent_posts(history):
        if (p.get("metrics") or {}).get("reach"):
            rows.setdefault(p.get("game"), []).append(p)
    sc, npost = {}, {}
    for g in active:
        s, n = _game_score(rows.get(g, []))
        sc[g], npost[g] = s, n

    cands = _news_candidates(strategy, active)

    def rank(c):
        rel = (2 if c.get("release_window") == "imminent"
               else 1 if (c.get("is_new_release") or c.get("release_window") == "recent")
               else 0)
        return (rel, int(c.get("momentum") or 0))

    cands.sort(key=rank, reverse=True)
    print(f"  [rotation] active={active} benched={[g for g in registry if g not in active]}")
    for g in active:
        print(f"    {g}: measured={npost[g]} score={sc[g]:.4f}")
    print(f"  [rotation] candidates={[(c['key'], rank(c)) for c in cands[:5]]}")

    if not cands:
        print("  [rotation] no valid candidates -> holding.")
        return False

    top = cands[0]
    is_release = rank(top)[0] > 0
    elig = [g for g in active if npost.get(g, 0) >= min_posts]
    worst = min(elig, key=lambda g: sc[g]) if elig else None
    share = False   # share = give the new game SOME slots (don't fully drop a game)

    if worst is None:
        if is_release and len(active) < max_active:
            worst = _most_slotted(strategy, active)
            share = True   # no perf data yet: ride the launch wave without dropping a proven game
            reason = f"launch wave (no data yet): give {top['key']} some of {worst}'s slots"
        else:
            print("  [rotation] not enough per-game data to judge yet -> holding.")
            return False
    elif is_release:
        reason = f"launch wave: {top['key']} replaces worst active {worst}"
    elif int(top.get("momentum") or 0) >= add_mom:
        if len(elig) < 2:
            print(f"  [rotation] only {len(elig)} game(s) have >= {min_posts} posts; "
                  "need >=2 measured games before a performance swap -> holding.")
            return False
        reason = f"perf swap: {worst} (worst) -> {top['key']} (momentum {top.get('momentum')})"
    else:
        print(f"  [rotation] top momentum {top.get('momentum')} < {add_mom} -> holding.")
        return False

    if not share and len([g for g in active if g != worst]) < min_active - 1:
        print("  [rotation] would drop below min_active -> holding.")
        return False

    add_key, add_name = top["key"], top["twitch_name"]
    worst_slots = [s for s in strategy.get("slots", []) if s.get("game") == worst]
    targets = worst_slots[: max(1, len(worst_slots) // 2)] if share else worst_slots
    for s in targets:
        s["game"] = add_key
        s["note"] = (f"{s.get('region', 'western')} {add_key} "
                     f"(rotated in {now_utc().date().isoformat()}: "
                     f"{str(top.get('why', ''))[:50]})")
    registry[add_key] = add_name
    strategy["games"] = registry
    # Persist scanner-suggested hashtags so a rotated-in game isn't captioned with
    # an empty tag pool (#22b).
    sug = [t for t in (top.get("suggested_hashtags") or []) if isinstance(t, str)][:6]
    if sug:
        gh = dict(strategy.get("game_hashtags", {}))
        gh[add_key] = sug
        strategy["game_hashtags"] = gh
    print(f"  [rotation] {'DRY-RUN ' if dry else ''}{'SHARE' if share else 'SWAP'} -> "
          f"{reason}; reassigned {len(targets)}/{len(worst_slots)} slot(s) "
          f"{worst} -> {add_key} ({add_name})")
    return True


def main():
    forced = "--strategize" in sys.argv
    never = "--no-strategize" in sys.argv
    dry = os.environ.get("DRY_RUN") == "1"

    strategy = load_strategy()
    history = load_history()

    # Went-dark alarm (#21): the poster's 'no clip found' days exit 0 (a green run),
    # so a silently-stopped account would never surface. If the newest post is older
    # than went_dark_hours, fail loudly so the failed Actions run emails the owner.
    posts = history.get("posts", [])
    newest = max((p.get("date_utc") for p in posts if p.get("date_utc")), default=None)
    if posts and newest and not dry:
        try:
            age_h = (now_utc() - datetime.datetime.fromisoformat(newest)).total_seconds() / 3600.0
        except Exception:
            age_h = None
        limit = float(strategy.get("went_dark_hours", 48))
        if age_h is not None and age_h > limit:
            sys.exit(f"WENT DARK: newest post is {age_h:.0f}h old (> {limit:.0f}h). "
                     "The account has stopped posting — check the poster runs, the "
                     "clip pool, and the IG token.")

    token = env("IG_ACCESS_TOKEN")
    ig = resolve_ig_user_id(token)

    print(f"[{BRAND_NAME}] analyze — {now_utc().date().isoformat()}")
    account = fetch_account(ig, token)
    followers = record_followers(account)
    print(f"Followers: {account.get('followers_count')} | media: {account.get('media_count')}")

    print("Measuring recent posts...")
    measured = measure_posts(history, token)
    yt_measured = measure_youtube(history)          # YouTube cross-post performance
    if not dry:
        save_history(history)
    print(f"Measured {measured} IG post(s), {yt_measured} YouTube video(s).")

    yt_ro = youtube_readout(history)
    if yt_ro:
        print("\nYouTube readout:")
        for k in sorted(yt_ro):
            v = yt_ro[k]
            print(f"  {k:>18}: n={v['videos']:>2} views={v['avg_views']} "
                  f"watch%={v['avg_watch_pct']} subs+={v['subs_gained']}")

    readout = ab_readout(history)
    if readout:
        print("\nA/B readout:")
        for k in sorted(readout):
            v = readout[k]
            print(f"  {k:>20}: n={v['posts']:>2} reach={v['avg_reach']:>7} "
                  f"ret={v['avg_retention']} vis/reach={v['visits_per_reach']} "
                  f"f/vis={v['follows_per_visit']} sh/reach={v['shares_per_reach']} "
                  f"f/reach={v['follows_per_reach']} f={v['total_follows']}")
    else:
        print("\nNo measured posts yet — A/B readout will populate after the first posts age 18h.")

    if never:
        print("--no-strategize set; leaving strategy.json untouched.")
        return
    if not (forced or _due(strategy)):
        print(f"\nStrategy review not due yet (next: {strategy.get('next_review')}).")
        return

    print("\nReviewing strategy (games + learnings)...")
    # Adaptive game rotation first (may reassign slots to a trending/new game),
    # then refresh the caption/selection learnings. Both mutate `strategy`.
    # Rotation is defensively wrapped — it must NEVER crash the daily analyzer.
    try:
        rotate_games(strategy, history, dry)
    except Exception as e:
        print(f"  [rotation] failed (non-fatal): {e}")

    try:
        strategy["learnings"] = call_strategist(strategy, readout, followers)
    except Exception as e:
        print(f"Strategist call failed, keeping current learnings: {e}")

    strategy["updated"] = now_utc().date().isoformat()
    strategy["next_review"] = (now_utc().date()
                               + datetime.timedelta(days=REVIEW_EVERY_DAYS)).isoformat()
    strategy["version"] = int(strategy.get("version", 1)) + 1

    if dry:
        print(f"\nDRY_RUN=1 — proposed strategy v{strategy['version']} NOT written "
              f"(learnings + any rotation above were simulated).")
        return

    with open(STRATEGY_FILE, "w", encoding="utf-8") as f:
        json.dump(strategy, f, indent=2, ensure_ascii=False)
    print(f"\nstrategy.json updated -> v{strategy['version']} "
          f"(games: {list(strategy.get('games', {}))}). Next review {strategy['next_review']}.")


if __name__ == "__main__":
    main()
