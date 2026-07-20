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


def send_message(text, timeout=60):
    return _call("sendMessage", {"chat_id": _chat(), "text": text,
                                 "disable_web_page_preview": True}, timeout)


def latest_update_id():
    """Highest update_id Telegram currently holds — a baseline so the poller only reads
    replies that arrive AFTER a proposal was sent (ignores anything older)."""
    j = _call("getUpdates", {"offset": -1, "timeout": 0})
    ups = j.get("result") or []
    return ups[-1]["update_id"] if ups else 0


def send_candidates(candidates, slot_label):
    """candidates = [{n, url (public mp4), title, broadcaster, game, views}] (already
    hosted somewhere public, e.g. R2). Sends each as a playable video + a how-to-reply
    note. Returns the baseline update_id so fulfill only accepts replies after this."""
    baseline = latest_update_id()
    if not send_message(f"🎬 New {slot_label} slot — which is best? Pick 1 of {len(candidates)}:").get("ok"):
        return None                                   # DM failed -> caller falls back
    for c in candidates:
        cap = (f"#{c['n']} — {(c.get('title') or '').strip()[:80]}\n"
               f"{c.get('broadcaster') or '?'} · {c.get('game') or '?'}"
               + (f" · {c['views']:,} views" if isinstance(c.get('views'), int) else ""))
        _call("sendVideo", {"chat_id": _chat(), "video": c["url"], "caption": cap,
                            "supports_streaming": True}, timeout=180)
    send_message("Reply like:  2 - clutch 1v4 with a knife\n"
                 "(the number is your pick; the rest becomes a one-line hint for the caption).\n"
                 "No reply in time → I'll auto-pick and post so the slot isn't missed.")
    return baseline


_PICK_RE = re.compile(r"^\s*#?\s*([1-9])\b[\s\-:.)]*\s*(.*)$", re.S)


def poll_decision(after_update_id, n_candidates):
    """Read the owner's reply picking a candidate. Returns (choice:int|None,
    description:str|None, last_update_id). Only messages from the configured chat that
    start with a valid candidate number count; the LATEST such reply wins."""
    j = _call("getUpdates", {"offset": (after_update_id or 0) + 1, "timeout": 0})
    ups = j.get("result") or []
    last = after_update_id or 0
    choice = desc = None
    for u in ups:
        last = max(last, u["update_id"])
        msg = u.get("message") or u.get("edited_message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(_chat()):
            continue
        m = _PICK_RE.match((msg.get("text") or "").strip())
        if m and 1 <= int(m.group(1)) <= n_candidates:
            choice = int(m.group(1))
            desc = (m.group(2) or "").strip() or None
    return choice, desc, last
