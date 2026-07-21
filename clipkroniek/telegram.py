#!/usr/bin/env python3
"""
Telegram human-in-the-loop for the poster (optional). At a due slot the poster sends
the top-3 candidate clips to the owner's phone; the owner replies "<1/2/3> <one-line
description>"; the fulfill step builds THAT clip (grounding the caption with the
description) and posts it. If no reply within the response window, the poster falls
back to the autonomous pick so the account never goes dark.

$0 (Telegram Bot API is free). No server: replies are read by POLLING getUpdates from a
short cron. Gated on TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID; a no-op if either is unset.
Non-fatal throughout — any Telegram failure just means we fall back to autonomous.
"""
import os
import re

import requests

_API = "https://api.telegram.org/bot{token}/{method}"


def configured():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def _token():
    return os.environ["TELEGRAM_BOT_TOKEN"]


def _chat():
    return os.environ["TELEGRAM_CHAT_ID"]


def _call(method, payload, timeout=60):
    try:
        r = requests.post(_API.format(token=_token(), method=method), json=payload, timeout=timeout)
        j = r.json()
        if not j.get("ok"):
            print(f"  telegram {method} failed {r.status_code}: {str(j.get('description'))[:160]}")
        return j
    except Exception as e:
        print(f"  telegram {method} error: {e}")
        return {}


def _upload(method, data, files, timeout=300):
    """Multipart upload (bots may upload up to 50 MB directly). Preferred over passing a
    URL: Telegram fetching a remote URL is capped at ~20 MB and fails on slow/large files
    ('failed to get HTTP URL content')."""
    try:
        r = requests.post(_API.format(token=_token(), method=method),
                          data=data, files=files, timeout=timeout)
        j = r.json()
        if not j.get("ok"):
            print(f"  telegram {method} upload failed {r.status_code}: "
                  f"{str(j.get('description'))[:160]}")
        return j
    except Exception as e:
        print(f"  telegram {method} upload error: {e}")
        return {}


def send_message(text, timeout=60):
    return _call("sendMessage", {"chat_id": _chat(), "text": text,
                                 "disable_web_page_preview": True}, timeout)


def latest_update_id():
    """Highest update_id Telegram currently holds — a baseline so the poller only reads
    replies that arrive AFTER a proposal was sent (ignores anything older)."""
    j = _call("getUpdates", {"offset": -1, "timeout": 0})
    ups = j.get("result") or []
    return ups[-1]["update_id"] if ups else 0


MAX_UPLOAD_MB = 49          # Telegram bot upload ceiling is 50 MB


def send_candidates(candidates, slot_label):
    """candidates = [{n, path (local mp4), title, broadcaster, game, views, url?}].
    Uploads each clip DIRECTLY (multipart) so Telegram never has to fetch a remote URL;
    falls back to the `url` if a direct upload isn't possible. Returns the baseline
    update_id so fulfill only accepts replies that arrive after this proposal."""
    baseline = latest_update_id()
    if not send_message(f"🎬 New {slot_label} slot — which is best? "
                        f"Pick 1 of {len(candidates)}:").get("ok"):
        return None                                   # DM failed -> caller falls back
    sent = 0
    for c in candidates:
        cap = (f"#{c['n']} — {(c.get('title') or '').strip()[:80]}\n"
               f"{c.get('broadcaster') or '?'} · {c.get('game') or '?'}"
               + (f" · {c['views']:,} views" if isinstance(c.get('views'), int) else ""))
        path, ok = c.get("path"), False
        if path and os.path.exists(path):
            mb = os.path.getsize(path) / (1024 * 1024)
            if mb <= MAX_UPLOAD_MB:
                with open(path, "rb") as f:
                    ok = bool(_upload("sendVideo",
                                      {"chat_id": _chat(), "caption": cap,
                                       "supports_streaming": "true"},
                                      {"video": (os.path.basename(path), f, "video/mp4")}
                                      ).get("ok"))
            else:
                print(f"  clip #{c['n']} is {mb:.0f} MB (> {MAX_UPLOAD_MB}) — trying URL")
        if not ok and c.get("url"):                   # fallback: let Telegram fetch it
            ok = bool(_call("sendVideo", {"chat_id": _chat(), "video": c["url"],
                                          "caption": cap, "supports_streaming": True},
                            timeout=180).get("ok"))
        if ok:
            sent += 1
        else:
            print(f"  could not deliver clip #{c['n']} to Telegram")
    if not sent:
        return None                                   # nothing arrived -> caller falls back
    send_message("Reply with your pick, optionally a CUT, then a one-line hint:\n"
                 "  2 - clutch 1v4 with a knife        (I choose the cut)\n"
                 "  2 15-28 clutch 1v4                 (keep 15s → 28s)\n"
                 "  2 0:15-0:28 clutch 1v4             (mm:ss also works)\n"
                 "  2 full clutch 1v4                  (post the whole clip)\n"
                 "The hint becomes the caption. No reply in time → I auto-pick and post "
                 "so the slot isn't missed.")
    return baseline


_PICK_RE = re.compile(r"^\s*#?\s*([1-9])\b[\s\-:.)]*\s*(.*)$", re.S)
# an optional cut right after the number: "15-28", "0:15-0:28", "15 to 28"
_SPAN_RE = re.compile(r"^\s*(\d{1,3}(?::\d{2})?(?:\.\d+)?)\s*(?:-|–|—|to)\s*"
                      r"(\d{1,3}(?::\d{2})?(?:\.\d+)?)\s*(.*)$", re.S | re.I)
# ...or an explicit "keep it whole"
_WHOLE_RE = re.compile(r"^\s*(full|whole|all|alles|heel)\b[\s\-:.,]*(.*)$", re.S | re.I)


def _to_sec(tok):
    tok = tok.strip()
    if ":" in tok:
        m, s = tok.split(":", 1)
        return int(m) * 60 + float(s)
    return float(tok)


def parse_reply(text, n_candidates):
    """Parse '<n> [cut] [description]'. Returns (choice, span, description) where span is
    (start_s, end_s) for an explicit cut, the string 'whole' to disable trimming, or None
    to let the pipeline decide. Lets the owner say WHICH PART to keep, since they're
    watching the clip anyway and the auto-trim can pick the wrong moment."""
    m = _PICK_RE.match((text or "").strip())
    if not m or not (1 <= int(m.group(1)) <= n_candidates):
        return None, None, None
    choice, rest = int(m.group(1)), (m.group(2) or "")
    span = None
    w = _WHOLE_RE.match(rest)
    sp = _SPAN_RE.match(rest)
    if w:
        span, rest = "whole", w.group(2)
    elif sp:
        try:
            a, b = _to_sec(sp.group(1)), _to_sec(sp.group(2))
            if b > a >= 0:
                span, rest = (a, b), sp.group(3)
        except Exception:
            pass
    return choice, span, (rest or "").strip() or None


def poll_decision(after_update_id, n_candidates):
    """Read the owner's reply picking a candidate. Returns (choice:int|None, span,
    description:str|None, last_update_id). Only messages from the configured chat that
    start with a valid candidate number count; the LATEST such reply wins."""
    j = _call("getUpdates", {"offset": (after_update_id or 0) + 1, "timeout": 0})
    ups = j.get("result") or []
    last = after_update_id or 0
    choice = span = desc = None
    for u in ups:
        last = max(last, u["update_id"])
        msg = u.get("message") or u.get("edited_message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(_chat()):
            continue
        c, sp, d = parse_reply(msg.get("text") or "", n_candidates)
        if c is not None:
            choice, span, desc = c, sp, d
    return choice, span, desc, last
