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
from clippost import (
    BRAND_NAME, CLAUDE_MODEL, STRATEGY_FILE,
    env, http, now_utc, load_strategy, load_history, save_history,
    resolve_ig_user_id, _graph_node,
)

FOLLOWERS_FILE = "followers.json"
REVIEW_EVERY_DAYS = 7
REMEASURE_WITHIN_DAYS = 14
MIN_AGE_HOURS_TO_MEASURE = 18

# Reel-valid insight metrics that also work below 100 followers.
MEDIA_METRICS = ["reach", "likes", "comments", "saved", "shares",
                 "total_interactions", "views", "ig_reels_avg_watch_time", "follows"]


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
        measured += 1
        print(f"  {post.get('game')}/{post.get('region')}: reach={m.get('reach')} "
              f"shares={m.get('shares')} saved={m.get('saved')} follows={m.get('follows')}")
    return measured


def _ratio(n, d):
    return round(n / d, 4) if d else None


def ab_readout(history):
    groups = {}
    for p in history.get("posts", []):
        m = p.get("metrics") or {}
        reach = m.get("reach") or 0
        if not reach:
            continue
        src = p.get("source", "twitch")
        cur = "curated" if p.get("curated") else "general"
        keys = [f"region:{p.get('region')}", f"game:{p.get('game')}",
                f"source:{src}", f"curation:{cur}", f"{src}|{p.get('game')}",
                f"{p.get('region')}|{p.get('game')}"]
        for k in keys:
            groups.setdefault(k, []).append((reach, m))

    def agg(items):
        n = len(items)
        R = sum(r for r, _ in items)
        sh = sum((m.get("shares") or 0) for _, m in items)
        sv = sum((m.get("saved") or 0) for _, m in items)
        fo = sum((m.get("follows") or 0) for _, m in items)
        return {"posts": n, "avg_reach": round(R / n, 1),
                "shares_per_reach": _ratio(sh, R), "saves_per_reach": _ratio(sv, R),
                "follows_per_reach": _ratio(fo, R), "total_follows": fo}
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
        "'Clipkroniek' (reposts GTA V + VALORANT clips WITH creator credit). The goal "
        "is reach that converts to FOLLOWS. You are given an A/B readout by region "
        "(western = English-language source clips vs asian = Asian-language source "
        "clips) and by game. Write a concise 'learnings' paragraph (UNDER 120 words) "
        "with concrete guidance for the next cycle: which region and which game "
        "convert best (rank by follows_per_reach, then shares_per_reach), what to do "
        "more/less of, and whether to shift the Western/Asian mix or the GTA/VALORANT "
        "mix. If data is thin, say so and advise holding the current mix. Return ONLY "
        "the learnings text — no JSON, no preamble."
    )
    payload = {"current_followers": cur, "ab_readout": readout,
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
def _game_score(rows):
    """Score one game's measured posts. Goal is FOLLOWS; shares are the strongest
    reach signal; avg reach is a tiebreak. Returns (score, n_measured)."""
    n = len(rows)
    R = sum((m.get("reach") or 0) for m in rows)
    if not n or not R:
        return 0.0, n
    fo = sum((m.get("follows") or 0) for m in rows)
    sh = sum((m.get("shares") or 0) for m in rows)
    score = (fo / R) * 10000 + (sh / R) * 1000 + (R / n) * 0.001
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

    def resolves(name):
        if not (cid and secret):
            return True  # can't validate without creds -> trust the scanner
        try:
            if tok[0] is None:
                tok[0] = twitch.get_app_token(cid, secret, http)
            twitch.resolve_game_id(name, cid, tok[0], http)
            return True
        except Exception:
            return False

    out, seen = [], set(active)
    for c in raw:
        if not isinstance(c, dict):
            continue
        k = (c.get("key") or "").strip().lower().replace(" ", "_")
        name = (c.get("twitch_name") or "").strip()
        if not k or not name or k in seen:
            continue
        if not resolves(name):
            print(f"    (candidate {k} '{name}' not found on Twitch; skipping)")
            continue
        c["key"], c["twitch_name"] = k, name
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
    for p in history.get("posts", []):
        m = p.get("metrics") or {}
        if m.get("reach"):
            rows.setdefault(p.get("game"), []).append(m)
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

    if worst is None:
        if is_release and len(active) < max_active:
            worst = _most_slotted(strategy, active)   # share slots, don't drop on perf
            reason = f"launch wave: add {top['key']}, sharing slots from {worst}"
        else:
            print("  [rotation] not enough per-game data to judge yet -> holding.")
            return False
    elif is_release:
        reason = f"launch wave: {top['key']} replaces worst active {worst}"
    elif int(top.get("momentum") or 0) >= add_mom:
        reason = f"perf swap: {worst} (worst) -> {top['key']} (momentum {top.get('momentum')})"
    else:
        print(f"  [rotation] top momentum {top.get('momentum')} < {add_mom} -> holding.")
        return False

    if len([g for g in active if g != worst]) < min_active - 1:
        print("  [rotation] would drop below min_active -> holding.")
        return False

    add_key, add_name = top["key"], top["twitch_name"]
    registry[add_key] = add_name
    moved = 0
    for s in strategy.get("slots", []):
        if s.get("game") == worst:
            s["game"] = add_key
            s["note"] = (f"{s.get('region', 'western')} {add_key} "
                         f"(rotated in {now_utc().date().isoformat()}: "
                         f"{str(top.get('why', ''))[:50]})")
            moved += 1
    strategy["games"] = registry
    print(f"  [rotation] {'DRY-RUN ' if dry else ''}SWAP -> {reason}; "
          f"reassigned {moved} slot(s) {worst} -> {add_key} ({add_name})")
    return True


def main():
    forced = "--strategize" in sys.argv
    never = "--no-strategize" in sys.argv
    dry = os.environ.get("DRY_RUN") == "1"

    strategy = load_strategy()
    history = load_history()
    token = env("IG_ACCESS_TOKEN")
    ig = resolve_ig_user_id(token)

    print(f"[{BRAND_NAME}] analyze — {now_utc().date().isoformat()}")
    account = fetch_account(ig, token)
    followers = record_followers(account)
    print(f"Followers: {account.get('followers_count')} | media: {account.get('media_count')}")

    print("Measuring recent posts...")
    measured = measure_posts(history, token)
    if not dry:
        save_history(history)
    print(f"Measured {measured} post(s).")

    readout = ab_readout(history)
    if readout:
        print("\nA/B readout:")
        for k in sorted(readout):
            v = readout[k]
            print(f"  {k:>18}: posts={v['posts']:>2} avg_reach={v['avg_reach']:>8} "
                  f"shares/reach={v['shares_per_reach']} "
                  f"follows/reach={v['follows_per_reach']} follows={v['total_follows']}")
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
    rotate_games(strategy, history, dry)

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
