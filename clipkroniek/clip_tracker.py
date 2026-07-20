#!/usr/bin/env python3
"""
Twitch clip performance tracker.

Every ~30 min it registers the top FRESH clips — per configured game AND across the
current top games (platform-wide) — and snapshots each clip's view_count at age
milestones, building a dataset of how a clip's views DEVELOP over its first day (to
test whether early velocity predicts virality → a sharper selection signal).

Design:
  - Denser EARLY milestones (0.5/1/1.5/2/3h) where virality is decided, then 4/8/12h, then
    a denser LATE leg (14/16/18/20/22/24h) to resolve the big 12->24h build the top clips show.
  - CONTROL sample: a small standing set of mid-ranked clips per game, tracked full 24h
    and EXEMPT from pruning, so we can compare "hot start" vs "mid start" (a baseline).
  - PRUNE after `prune_after_h` (1.5h): keep only the top `keep_top_per_game` clips per
    game, ranked by their views AT the prune milestone (a fair same-age comparison), so
    we stop spending snapshots on the dead long tail. Pruned clips are archived with
    their partial trajectory.
  - EXTENDED follow-up: at the 24h boundary the top `extended.top_n` clips (ranked by
    their 24h views, platform-wide) keep being snapshotted every `interval_h` up to
    `until_h` — to see how the durable winners develop past day one. It's a rolling
    leaderboard: a clip bumped out of the top-N is archived ("extended_bumped"); one
    followed to the cap is archived ("extended_end"). The rest complete at 24h.
  - DELETED clips (vanish from the API) are marked after N misses (a signal in itself).
  - game_rank (position in the top-games list) is stored so views are comparable across
    a huge game vs a niche one.
  - State read is GUARDED: a transient R2 error aborts the run rather than overwriting
    good state with an empty one.

State + dataset in R2 (no git noise): tracker/tracking.json (active) +
tracker/dataset-<date>.jsonl (completed). $0 (Twitch + public-repo Actions free).
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
    """Return the object text, "" if genuinely absent, or RAISE on a real error so the
    run aborts instead of clobbering good state with an empty write (#1)."""
    try:
        obj = _r2_client().get_object(Bucket=env("R2_BUCKET"), Key=key)
        return obj["Body"].read().decode("utf-8")
    except Exception as e:
        resp = getattr(e, "response", None) or {}
        code = (resp.get("Error") or {}).get("Code")
        status = (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if code in ("NoSuchKey", "NoSuchBucket", "404", "NotFound") or status == 404:
            return ""
        raise


def _r2_put_text(key, text, ct="application/json"):
    _r2_client().put_object(Bucket=env("R2_BUCKET"), Key=key,
                            Body=text.encode("utf-8"), ContentType=ct)


def _age_h(rec, now):
    created = _parse_ts(rec.get("created_at"))
    return (now - created).total_seconds() / 3600.0 if created else None


def _snap_views(rec, milestone):
    for s in rec.get("snapshots", []):
        if s.get("target_h") == milestone:
            return s.get("views")
    return None


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

    milestones = sorted(float(m) for m in cfg.get(
        "milestones_h", [0.5, 1, 1.5, 2, 3, 4, 8, 12, 14, 16, 18, 20, 22, 24]))
    per_game = int(cfg.get("per_game_track", 12))
    control_target = int(cfg.get("control_per_game", 5))
    win_min = int(cfg.get("discover_window_min", 45))
    prune_after = float(cfg.get("prune_after_h", 1.5))
    prune_ms = float(cfg.get("prune_by_milestone", 1.5))
    keep_top = int(cfg.get("keep_top_per_game", 25))
    del_misses = int(cfg.get("delete_after_misses", 2))

    # Extended follow-up: keep snapshotting the top-N clips (by 24h views) beyond 24h,
    # every `interval_h`, up to `until_h` — to see how the durable winners develop.
    ext_cfg = cfg.get("extended", {}) or {}
    ext_on = bool(ext_cfg.get("enabled", True))
    ext_top = int(ext_cfg.get("top_n", 10))
    ext_int = float(ext_cfg.get("interval_h", 2))
    ext_until = float(ext_cfg.get("until_h", 72))
    ext_ms = []
    if ext_on:
        m = 24.0 + ext_int
        while m <= ext_until + 1e-9:
            ext_ms.append(round(m, 3))
            m += ext_int

    tracking = {}
    txt = _r2_get_text(STATE_KEY)          # guarded — raises on a real R2 error (#1)
    if txt:
        tracking = json.loads(txt)

    # --- games to scan, with rank (#5: rank = popularity proxy) -----------------
    scan = {}                              # name -> {"id","set","rank"}
    for i, g in enumerate(twitch.get_top_games(
            cid, token, http, first=int(cfg.get("platform_top_games", 20)))):
        if g.get("id") and g.get("name"):
            scan[g["name"]] = {"id": g["id"], "set": "platform", "rank": i + 1}
    for name in cfg.get("games", []):
        found = twitch.find_game(name, cid, token, http)
        if found:
            existing = scan.get(found[1], {})
            scan[found[1]] = {"id": found[0], "set": "game",
                              "rank": existing.get("rank")}

    started = (now - datetime.timedelta(minutes=win_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ended = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # standing control counts per game (to keep a bounded control set, #4)
    control_now = {}
    for r in tracking.values():
        if r.get("control"):
            control_now[r.get("game")] = control_now.get(r.get("game"), 0) + 1

    def _register(c, name, meta, control):
        k = c.get("id")
        if not k or k in tracking:
            return False
        tracking[k] = {"game": name, "game_id": meta["id"], "set": meta["set"],
                       "game_rank": meta["rank"], "broadcaster": c.get("broadcaster_name"),
                       "creator": c.get("creator_name"), "title": c.get("title"),
                       "language": c.get("language"), "created_at": c.get("created_at"),
                       "control": control, "misses": 0, "snapshots": []}
        return True

    registered = ctrl_added = 0
    for name, meta in scan.items():
        try:
            clips = twitch.get_recent_clips(meta["id"], started, ended, cid, token, http, pages=1)
        except Exception as e:
            print(f"  (discover {name} failed: {e})")
            continue
        clips.sort(key=lambda c: int(c.get("view_count") or 0), reverse=True)
        for c in clips[:per_game]:
            if _register(c, name, meta, False):
                registered += 1
        # control: top up the standing mid-ranked set for this game
        need = max(0, control_target - control_now.get(name, 0))
        if need:
            pool = clips[per_game:]
            step = max(1, len(pool) // need) if pool else 1
            for c in pool[::step][:need]:
                if _register(c, name, meta, True):
                    ctrl_added += 1
                    control_now[name] = control_now.get(name, 0) + 1

    # --- snapshot every active clip at any passed milestone --------------------
    ids = list(tracking.keys())
    current = twitch.get_clips_by_id(ids, cid, token, http) if ids else {}
    done = {}                               # key -> reason
    for k, rec in list(tracking.items()):
        age = _age_h(rec, now)
        if age is None:
            done[k] = "no_created_at"
            continue
        cur = current.get(k)
        if cur is None:                     # #2: clip not returned -> maybe deleted
            rec["misses"] = rec.get("misses", 0) + 1
            if rec["misses"] >= del_misses:
                done[k] = "deleted"
            continue
        rec["misses"] = 0
        vc = int(cur["view_count"]) if cur.get("view_count") is not None else None
        want = list(milestones) + (ext_ms if rec.get("extended") else [])
        recorded = {s["target_h"] for s in rec["snapshots"]}
        for m in want:
            if age >= m and m not in recorded and vc is not None:
                rec["snapshots"].append({"target_h": m, "age_h": round(age, 3), "views": vc})

    # --- prune: keep only the top-N per game past the checkpoint (control exempt) --
    pool_by_game = {}
    for k, rec in tracking.items():
        if k in done or rec.get("control"):
            continue
        age = _age_h(rec, now)
        pv = _snap_views(rec, prune_ms)
        if age is not None and age >= prune_after and pv is not None:
            pool_by_game.setdefault(rec.get("game"), []).append((k, pv))
    for game, lst in pool_by_game.items():
        lst.sort(key=lambda t: t[1], reverse=True)
        for k, _ in lst[keep_top:]:         # everyone below the cut is dropped
            done[k] = "pruned"

    # --- 24h boundary: extend the top-N (by 24h views) beyond 24h, else complete --
    if ext_on:
        at24 = []
        for k, rec in tracking.items():
            if k in done:
                continue
            v24 = _snap_views(rec, 24.0)
            if v24 is None:                 # not at 24h yet -> keep tracking normally
                continue
            age = _age_h(rec, now)
            if age is not None and age >= ext_until:
                done[k] = "extended_end"    # followed to the cap -> finalise
                continue
            at24.append((k, v24))
        at24.sort(key=lambda t: t[1], reverse=True)
        keep = set(k for k, _ in at24[:ext_top])
        for k, _ in at24:
            if k in keep:
                tracking[k]["extended"] = True
            else:
                done[k] = "extended_bumped" if tracking[k].get("extended") else "24h"
    else:
        for k, rec in tracking.items():
            if k in done:
                continue
            age = _age_h(rec, now)
            if age is not None and age >= milestones[-1]:
                done[k] = "24h"

    # --- archive completed trajectories to the day's dataset -------------------
    n_done = {}
    by_day = {}
    for k, reason in done.items():
        rec = tracking.pop(k, None)
        if not rec:
            continue
        n_done[reason] = n_done.get(reason, 0) + 1
        day = now.date().isoformat()
        by_day.setdefault(day, []).append(json.dumps(
            {"clip_id": k, "completed_at": now.isoformat(), "reason": reason, **rec},
            ensure_ascii=False))
    for day, lines in by_day.items():
        key = f"tracker/dataset-{day}.jsonl"
        existing = _r2_get_text(key)
        _r2_put_text(key, existing + "\n".join(lines) + "\n", "application/x-ndjson")

    _r2_put_text(STATE_KEY, json.dumps(tracking, ensure_ascii=False))
    base = env("R2_PUBLIC_BASE_URL").rstrip("/")
    print(f"tracker: +{registered} new (+{ctrl_added} control), {len(tracking)} active, "
          f"completed {dict(n_done)} | games {len(scan)} | {base}/tracker/dataset-<date>.jsonl")


if __name__ == "__main__":
    main()
