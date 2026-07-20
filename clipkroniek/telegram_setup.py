#!/usr/bin/env python3
"""
One-time helper to find your Telegram chat_id.

Steps:
  1) In Telegram, message @BotFather -> /newbot -> get the BOT TOKEN.
  2) Open your new bot and send it ANY message (e.g. "hi").
  3) Run:  TELEGRAM_BOT_TOKEN=<token> python telegram_setup.py
     -> it prints your chat_id. Add it as the TELEGRAM_CHAT_ID secret.
"""
import os
import sys

import requests

tok = os.environ.get("TELEGRAM_BOT_TOKEN")
if not tok:
    sys.exit("Set TELEGRAM_BOT_TOKEN first (from @BotFather).")

j = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=30).json()
ups = j.get("result") or []
if not ups:
    sys.exit("No messages yet — open your bot in Telegram, send it any message, then re-run.")

seen = {}
for u in ups:
    m = u.get("message") or u.get("edited_message") or {}
    c = m.get("chat") or {}
    if c.get("id") is not None:
        seen[c["id"]] = f"{c.get('first_name') or ''} @{c.get('username') or '-'} ({c.get('type')})"
for cid, who in seen.items():
    print(f"TELEGRAM_CHAT_ID = {cid}   <-  {who.strip()}")
