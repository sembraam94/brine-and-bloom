#!/usr/bin/env python3
"""
Twitch clip performance tracker.

Snapshots top FRESH clips' view counts at age milestones (0.5h, 1h, 4h, 8h, 12h, 24h)
to build a dataset of how a clip's views DEVELOP over its first day — game-specific AND
platform-wide (a sweep of the current top games). Feeds the question: does early
velocity predict which clips get big (a sharper selection signal than one snapshot).

State + dataset live in R2 (no git noise; $0 — Twitch + Actions are free):
  tracker/tracking.json          active clips still inside their 24h window
  tracker/dataset-<date>.jsonl   completed trajectories (one clip per line), by day

Runs every ~30 min so the 30/60-min milestones are captured. Config: strategy.tracker.
"""
import os
import sys
import json
import datetime

import twitch
from clippost import (env, http, now_utc, _parse_ts, _r2_client, r2_configured,
                      load_strategy)

STATE_KEY = "tracker/tracking.json"


def _r2_get_text(key):
    try:
        obj = _r2_client().get_object(Bucket=env("R2_BUCKET"), Key=key)
        return obj["Body"].read().decode("utf-8")
    except Exception:
        return ""


def _r2_put_text(key, text, ct="application/json"):
    _r2_client().put_object(Bucket=env("R2_BUCKET"), Key=key,
                            Body=text.encode("utf-8"), ContentType=ct)


def main():
    strategy = load_strategy()
    cfg = strategy.get("tracker", {}) or {}
    if not cfg.get("enabled", True):
        print("tracker: disabled in strategy.json")
        return
    if not r2_configured():
        sys.exit("tracker needs R2 (state lives there).")

    cid = env("TWITCH_CLIENT_ID")
    secret = env("TWITCH_CLIENT_SECRET")
    token = twitch.get_app_token(cid, secret, http)
    now = now_utc()
    milestones = sorted(float(m) for m in cfg.get("milestones_h", [0.5, 1, 4, 8, 12, 24]))
    per_game = int(cfg.get("per_game_track", 12))
    win_min = int(cfg.get("discover_window_min", 45))

    tracking = {}
    txt = _r2_get_text(STATE_KEY)
    if txt:
        try:
            tracking = json.loads(txt)
        except Exception:
            tracking = {}

    # --- which games to scan: configured games (game-specific) + top games (platform)
    scan = {}                                     # name -> (game_id, set_label)
    for g in twitch.get_top_games(cid, token, http,
                                  first=int(cfg.get("platform_top_games", 20))):
        if g.get("id") and g.get("name"):
            scan[g["name"]] = (g["id"], "platform")
    for name in cfg.get("games", []):             # configured games take the 'game' label
        found = twitch.find_game(name, cid, token, http)
        if found:
            scan[found[1]] = (found[0], "game")

    started = (now - datetime.timedelta(minutes=win_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ended = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- discovery: register the top fresh clips per game -----------------------
    registered = 0
    for name, (gid, label) in scan.items():
        try:
            clips = twitch.get_recent_clips(gid, started, ended, cid, token, http, pages=1)
        except Exception as e:
            print(f"  (discover {name} failed: {e})")
            continue
        clips.sort(key=lambda c: int(c.get("view_count") or 0), reverse=True)
        for c in clips[:per_game]:
            k = c.get("id")
            if not k or k in tracking:
                continue
            tracking[k] = {
                "game": name, "game_id": gid, "set": label,
                "broadcaster": c.get("broadcaster_name"),
                "creator": c.get("creator_name"), "title": c.get("title"),
                "language": c.get("language"), "created_at": c.get("created_at"),
                "snapshots": [],
            }
            registered += 1

    # --- snapshot: current view_count for every active clip at passed milestones -
    ids = list(tracking.keys())
    current = twitch.get_clips_by_id(ids, cid, token, http) if ids else {}
    completed = []
    for k, rec in list(tracking.items()):
        created = _parse_ts(rec.get("created_at"))
        if not created:
            completed.append(k)
            continue
        age_h = (now - created).total_seconds() / 3600.0
        recorded = {s["target_h"] for s in rec["snapshots"]}
        cur = current.get(k)
        vc = int(cur["view_count"]) if (cur and cur.get("view_count") is not None) else None
        for m in milestones:
            if age_h >= m and m not in recorded and vc is not None:
                rec["snapshots"].append({"target_h": m, "age_h": round(age_h, 3),
                                         "views": vc})
        if age_h >= milestones[-1]:
            completed.append(k)

    # --- move completed trajectories to the day's dataset file -----------------
    n_done = 0
    if completed:
        by_day = {}
        for k in completed:
            rec = tracking.pop(k, None)
            if not rec:
                continue
            day = (now.date().isoformat())
            by_day.setdefault(day, []).append(
                json.dumps({"clip_id": k, "completed_at": now.isoformat(), **rec},
                           ensure_ascii=False))
            n_done += 1
        for day, lines in by_day.items():
            key = f"tracker/dataset-{day}.jsonl"
            existing = _r2_get_text(key)
            _r2_put_text(key, existing + "\n".join(lines) + "\n", "application/x-ndjson")

    _r2_put_text(STATE_KEY, json.dumps(tracking, ensure_ascii=False))
    base = env("R2_PUBLIC_BASE_URL").rstrip("/")
    print(f"tracker: +{registered} new, {len(tracking)} active, {n_done} completed "
          f"| games scanned: {len(scan)} | dataset: {base}/tracker/dataset-<date>.jsonl")


if __name__ == "__main__":
    main()
