#!/usr/bin/env python3
"""
Brine & Bloom — performance analyzer + strategist.

This is the MEASURE-AND-ADAPT half of the loop. It runs once a day on GitHub
Actions and does three things:

  1. Tracks growth: reads the live follower count and appends it to
     followers.json (works at any account size via the followers_count field).
  2. Reviews every recent post: pulls its Instagram Insights (reach, saves,
     shares, views, follows, ...) and stores them on the post in history.json.
  3. Adapts (on a weekly cadence): hands the real numbers to Claude, which
     rewrites strategy.json — adjusting WHICH days/times to post, HOW MANY posts
     per week (within hard safety caps), the format mix, the theme weighting, and
     the accumulated "learnings" that every future caption is written against.

So the account literally learns what works and shifts toward it.

Env: ANTHROPIC_API_KEY, IG_ACCESS_TOKEN  (IG_USER_ID optional — derived from token)
Flags:
    --strategize   force a strategy rewrite now (ignore the weekly cadence)
    --no-strategize  only collect metrics, never touch strategy.json
    DRY_RUN=1      print proposed strategy but do not write any files
"""

import os
import sys
import json
import datetime
import requests

# Reuse config + helpers from the poster so the two halves never drift apart.
from autopost import (
    BRAND_NAME, GRAPH_VERSION, CLAUDE_MODEL,
    STRATEGY_FILE, HISTORY_FILE,
    env, load_strategy, load_history, save_history, now_utc,
    _graph_base, _graph_node, resolve_ig_user_id,
    MAX_HASHTAGS,
)

FOLLOWERS_FILE = "followers.json"
REVIEW_EVERY_DAYS = 7          # how often Claude rewrites the strategy
REMEASURE_WITHIN_DAYS = 14     # keep refreshing metrics for posts this recent
MIN_AGE_HOURS_TO_MEASURE = 18  # let a post accumulate engagement before judging

# Media-insight metrics that are valid for IMAGE/feed posts AND work below 100
# followers (verified against Meta's IG Media Insights reference, 2026).
MEDIA_METRICS = [
    "reach", "likes", "comments", "saved", "shares",
    "total_interactions", "views", "profile_visits", "follows",
]

# Hard safety envelope. Claude proposes cadence; code clamps it so an over-eager
# strategy can never get a young account flagged. Caps widen as the account ages
# (a ramp — see README "Growth strategy & safety").
def cadence_caps(age_days):
    if age_days < 14:
        return {"max_ppw": 5, "max_ppd": 1}
    if age_days < 35:
        return {"max_ppw": 7, "max_ppd": 1}
    return {"max_ppw": 10, "max_ppd": 2}

MIN_POSTS_PER_WEEK = 3


# =============================================================================
# Growth tracking
# =============================================================================

def fetch_account(ig_id, token):
    """followers_count is a FIELD on the user node — works below 100 followers,
    unlike the follower_count *insights* metric (which needs 100+)."""
    r = requests.get(
        _graph_base(ig_id),
        params={"fields": "followers_count,follows_count,media_count,username",
                "access_token": token},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def record_followers(account):
    data = _load_followers()
    today = now_utc().date().isoformat()
    entry = {
        "date": today,
        "followers": account.get("followers_count"),
        "media_count": account.get("media_count"),
    }
    # one row per day; overwrite today's if it already exists
    data = [e for e in data if e.get("date") != today]
    data.append(entry)
    data.sort(key=lambda e: e["date"])
    if os.environ.get("DRY_RUN") != "1":
        with open(FOLLOWERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    return data


def _load_followers():
    if os.path.exists(FOLLOWERS_FILE):
        try:
            with open(FOLLOWERS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


# =============================================================================
# Per-post insights
# =============================================================================

def fetch_media_insights(media_id, token):
    """Request the metric batch; if the batch errors (e.g. a metric isn't valid
    for this media), fall back to one-by-one so a single bad metric never wipes
    the whole pull."""
    def _request(metrics):
        r = requests.get(
            f"{_graph_node(media_id)}/insights",
            params={"metric": ",".join(metrics), "access_token": token},
            timeout=60,
        )
        r.raise_for_status()
        out = {}
        for item in r.json().get("data", []):
            name = item.get("name")
            values = item.get("values") or [{}]
            val = values[0].get("value")
            if val is None:
                val = item.get("total_value", {}).get("value")
            out[name] = val
        return out

    try:
        return _request(MEDIA_METRICS)
    except requests.HTTPError:
        result = {}
        for m in MEDIA_METRICS:
            try:
                result.update(_request([m]))
            except requests.HTTPError:
                continue
        return result


def measure_posts(history, token):
    """Pull/refresh insights for recent published posts."""
    now = now_utc()
    measured = 0
    for post in history.get("posts", []):
        mid = post.get("media_id")
        if not mid:
            continue
        # only measure posts old enough to have data, and recent enough to bother
        try:
            posted = datetime.datetime.fromisoformat(post["date_utc"])
        except Exception:
            continue
        age_h = (now - posted).total_seconds() / 3600.0
        if age_h < MIN_AGE_HOURS_TO_MEASURE:
            continue
        if age_h > REMEASURE_WITHIN_DAYS * 24 and post.get("metrics"):
            continue  # old + already measured -> leave it frozen
        try:
            metrics = fetch_media_insights(mid, token)
        except requests.HTTPError as e:
            print(f"  insights failed for {mid}: {e}")
            continue
        post["metrics"] = metrics
        post["measured_at"] = now.isoformat()
        measured += 1
        print(f"  measured {post.get('title','?')[:40]}: "
              f"reach={metrics.get('reach')} saved={metrics.get('saved')} "
              f"shares={metrics.get('shares')} follows={metrics.get('follows')}")
    return measured


# =============================================================================
# Strategy adaptation
# =============================================================================

def _safe_ratio(num, den):
    return round(num / den, 4) if den else None


def summarize_performance(history):
    """Compact per-slot and per-theme aggregates for the strategist."""
    by_theme, by_slot = {}, {}
    rows = []
    for p in history.get("posts", []):
        m = p.get("metrics") or {}
        reach = m.get("reach") or 0
        if not reach:
            continue
        sends = m.get("shares") or 0
        saves = m.get("saved") or 0
        follows = m.get("follows") or 0
        row = {
            "theme": p.get("theme"),
            "format": p.get("format"),
            "weekday": p.get("weekday"),
            "slot_hour": p.get("slot_hour"),
            "reach": reach,
            "sends_per_reach": _safe_ratio(sends, reach),
            "saves_per_reach": _safe_ratio(saves, reach),
            "follows_per_reach": _safe_ratio(follows, reach),
        }
        rows.append(row)
        by_theme.setdefault(p.get("theme"), []).append(row)
        by_slot.setdefault(f"w{p.get('weekday')}-{p.get('slot_hour')}", []).append(row)

    def agg(group):
        out = {}
        for k, items in group.items():
            n = len(items)
            out[k] = {
                "posts": n,
                "avg_reach": round(sum(i["reach"] for i in items) / n, 1),
                "avg_sends_per_reach": round(
                    sum((i["sends_per_reach"] or 0) for i in items) / n, 4),
                "avg_saves_per_reach": round(
                    sum((i["saves_per_reach"] or 0) for i in items) / n, 4),
                "avg_follows_per_reach": round(
                    sum((i["follows_per_reach"] or 0) for i in items) / n, 4),
            }
        return out

    return {"by_theme": agg(by_theme), "by_slot": agg(by_slot),
            "measured_posts": len(rows)}


def follower_trajectory(followers):
    if not followers:
        return {"current": None, "gain_7d": None, "gain_14d": None}
    current = followers[-1].get("followers")

    def gain(days):
        if current is None or len(followers) < 2:
            return None
        cutoff = (now_utc().date() - datetime.timedelta(days=days)).isoformat()
        past = [e for e in followers if e["date"] <= cutoff]
        base = past[-1]["followers"] if past else followers[0].get("followers")
        if base is None:
            return None
        return current - base

    return {"current": current, "gain_7d": gain(7), "gain_14d": gain(14)}


def clamp_strategy(proposed, current, age_days):
    """Force the strategist's proposal into the hard safety envelope."""
    caps = cadence_caps(age_days)
    out = dict(current)                       # start from current, overlay proposal
    out.update({k: proposed.get(k, current.get(k))
                for k in ("brand_focus", "content_plan", "learnings",
                          "hashtag_strategy", "caption_strategy", "timezone")})

    cadence = proposed.get("cadence", {}) or {}
    ppw = int(cadence.get("posts_per_week",
                          current.get("cadence", {}).get("posts_per_week", 4)))
    ppd = int(cadence.get("max_posts_per_day",
                          current.get("cadence", {}).get("max_posts_per_day", 1)))
    ppw = max(MIN_POSTS_PER_WEEK, min(ppw, caps["max_ppw"]))
    ppd = max(1, min(ppd, caps["max_ppd"]))

    def _norm(s):
        return {
            "weekday": int(s["weekday"]) % 7,
            "hour": max(0, min(23, int(s["hour"]))),
            "minute": max(0, min(59, int(s.get("minute", 0)))),
            "format": "carousel" if s.get("format") == "carousel" else "image",
            "theme": str(s.get("theme", "general")),
            "note": str(s.get("note", "")),
        }

    cleaned = []
    for s in (proposed.get("slots") or []):
        try:
            cleaned.append(_norm(s))
        except (KeyError, ValueError, TypeError):
            continue

    # If the strategist returned fewer valid slots than the cadence, top up from
    # the current slots so the slot count always equals posts_per_week (otherwise
    # the account would silently under-post vs. its stated cadence).
    if len(cleaned) < ppw:
        for s in current.get("slots", []):
            if len(cleaned) >= ppw:
                break
            try:
                ns = _norm(s)
            except (KeyError, ValueError, TypeError):
                continue
            if not any(c["weekday"] == ns["weekday"] and c["hour"] == ns["hour"]
                       for c in cleaned):
                cleaned.append(ns)

    if not cleaned:                       # last-resort: keep the current schedule
        cleaned = [_norm(s) for s in current.get("slots", [])]

    cleaned = cleaned[:ppw]
    ppw = len(cleaned)                     # keep cadence and slot count consistent
    out["cadence"] = {"posts_per_week": ppw, "max_posts_per_day": ppd}
    out["slots"] = cleaned

    out["tolerance_minutes"] = int(current.get("tolerance_minutes", 90))
    out["jitter_minutes"] = int(current.get("jitter_minutes", 12))
    out["account_start_date"] = current.get("account_start_date")
    out["version"] = int(current.get("version", 0)) + 1
    out["updated"] = now_utc().date().isoformat()
    out["next_review"] = (
        now_utc().date() + datetime.timedelta(days=REVIEW_EVERY_DAYS)
    ).isoformat()
    return out


def call_strategist(current, perf, traj, age_days):
    caps = cadence_caps(age_days)
    system = f"""You are the growth strategist for the Instagram account "{BRAND_NAME}"
(cooking tips, recipes, marinades, AI-styled food photography). Your ONLY goal is to grow
followers as fast as is SAFE for a {age_days}-day-old account, by rewriting its posting
strategy based on real performance data.

How Instagram growth works in 2026 (use this):
- The top growth signals, in order: SENDS/shares (DM to a friend) > SAVES > watch-time/likes.
  Optimize the schedule and themes toward posts with high sends_per_reach and saves_per_reach.
- Reach to NON-followers is what grows a young account; follows_per_reach tells you if reach
  is converting to follows.
- Posting at fixed clock times looks robotic; we already add jitter, so keep slot minutes varied.
- Hashtags are a minor lever (max {MAX_HASHTAGS}); keyword-rich captions matter more.

SAFETY ENVELOPE you MUST stay within for a {age_days}-day-old account:
- posts_per_week between {MIN_POSTS_PER_WEEK} and {caps['max_ppw']}.
- max_posts_per_day at most {caps['max_ppd']}.
- Ramp cadence UP only when recent reach/saves/sends are healthy or improving; if growth is
  flat or engagement dropped, hold or REDUCE cadence. Never jump cadence aggressively.

Decision rules:
- Re-weight themes toward the top performers (by sends+saves per reach); retire themes in the
  bottom that have ≥3 posts and weak numbers.
- Keep/duplicate the best-performing posting slots (weekday+hour); drop the worst.
- Update "learnings" with concrete, current guidance the caption writer should apply next
  (what to do more of / less of). Keep it under ~120 words and specific.
- With little/no data, make only small changes and lean on the cold-start playbook
  (Reels/short save-bait content, search-friendly captions, consistency).

Return ONLY a JSON object with these keys (no markdown, no commentary):
{{
  "cadence": {{"posts_per_week": int, "max_posts_per_day": int}},
  "slots": [{{"weekday": 0-6 (0=Mon), "hour": 0-23, "minute": 0-59, "format": "image"|"carousel", "theme": "short-theme-key", "note": "why"}}],
  "content_plan": {{"theme-key": "what a post in this theme should be"}},
  "caption_strategy": "one paragraph of caption guidance",
  "hashtag_strategy": "one paragraph of hashtag guidance",
  "learnings": "concrete guidance learned from the data (<120 words)"
}}
The number of slots should equal posts_per_week."""

    payload = {
        "current_strategy": {
            "cadence": current.get("cadence"),
            "slots": current.get("slots"),
            "content_plan": current.get("content_plan"),
            "timezone": current.get("timezone"),
        },
        "follower_trajectory": traj,
        "performance": perf,
        "account_age_days": age_days,
    }

    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": 2500,
        "system": system,
        "messages": [{
            "role": "user",
            "content": "Here is the current strategy and the real performance "
                       "data. Rewrite the strategy to grow fastest while staying "
                       "safe.\n\n" + json.dumps(payload, ensure_ascii=False, indent=2),
        }],
    }
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": env("ANTHROPIC_API_KEY"),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=body,
        timeout=120,
    )
    resp.raise_for_status()
    text = "".join(
        b.get("text", "") for b in resp.json().get("content", [])
        if b.get("type") == "text"
    ).strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
        text = text.strip()
    return json.loads(text)


def strategy_due(strategy, forced):
    if forced:
        return True
    nxt = strategy.get("next_review")
    if not nxt:
        return True
    try:
        return now_utc().date() >= datetime.date.fromisoformat(nxt)
    except Exception:
        return True


def account_age_days(strategy):
    start = strategy.get("account_start_date")
    try:
        return max(0, (now_utc().date() - datetime.date.fromisoformat(start)).days)
    except Exception:
        return 0


# =============================================================================
# Orchestration
# =============================================================================

def main():
    forced = "--strategize" in sys.argv
    never = "--no-strategize" in sys.argv
    dry = os.environ.get("DRY_RUN") == "1"

    strategy = load_strategy()
    history = load_history()
    token = env("IG_ACCESS_TOKEN")
    ig_id = resolve_ig_user_id(token)

    print(f"[{BRAND_NAME}] analyze — {now_utc().date().isoformat()}")

    # 1) growth tracking
    account = fetch_account(ig_id, token)
    followers = record_followers(account)
    traj = follower_trajectory(followers)

    def _fmt_gain(g):
        return f"{g:+d}" if isinstance(g, int) else "n/a"
    print(f"Followers: {traj['current']} "
          f"(7d {_fmt_gain(traj['gain_7d'])}, 14d {_fmt_gain(traj['gain_14d'])})")

    # 2) per-post insights
    print("Measuring recent posts...")
    measured = measure_posts(history, token)
    if not dry:
        save_history(history)
    print(f"Measured {measured} post(s).")

    # 3) adapt the strategy (weekly, unless forced/disabled)
    age_days = account_age_days(strategy)
    if never:
        print("--no-strategize set; leaving strategy.json untouched.")
        return
    if not strategy_due(strategy, forced):
        print(f"Strategy review not due yet (next: {strategy.get('next_review')}).")
        return

    perf = summarize_performance(history)
    print(f"Reviewing strategy (age {age_days}d, {perf['measured_posts']} measured posts)...")
    try:
        proposed = call_strategist(strategy, perf, traj, age_days)
    except Exception as e:
        sys.exit(f"Strategist call failed, keeping current strategy: {e}")

    new_strategy = clamp_strategy(proposed, strategy, age_days)

    if dry:
        print("\nDRY_RUN=1 — proposed strategy (not written):")
        print(json.dumps(new_strategy, ensure_ascii=False, indent=2))
        return

    with open(STRATEGY_FILE, "w", encoding="utf-8") as f:
        json.dump(new_strategy, f, indent=2, ensure_ascii=False)
    print(f"strategy.json updated -> v{new_strategy['version']} "
          f"({new_strategy['cadence']['posts_per_week']}/wk, "
          f"{len(new_strategy['slots'])} slots). Next review {new_strategy['next_review']}.")


if __name__ == "__main__":
    main()
