#!/usr/bin/env python3
"""
Clipkroniek IG token health check — read-only, never posts.

Confirms the CK Instagram token is valid and carries the scopes the poster needs:
  - identity (instagram_business_basic)
  - reading a post's comments edge, which is gated by
    instagram_business_manage_comments — so a clean 200 here proves that scope is
    granted (basic alone cannot read the /comments edge), which is exactly what the
    auto first-comment feature (#4) requires.

Run via the 'Clipkroniek — token health check' workflow (uses CK_IG_ACCESS_TOKEN)
or locally with IG_ACCESS_TOKEN set. Exits non-zero if the token is invalid or the
comments scope is missing, so a failed run is visibly red.
"""
import os
import sys
import requests

BASE = "https://graph.instagram.com/v23.0"


def _get(path, token, **params):
    params["access_token"] = token
    return requests.get(f"{BASE}/{path}", params=params, timeout=30)


def main():
    token = os.environ.get("IG_ACCESS_TOKEN")
    if not token:
        sys.exit("IG_ACCESS_TOKEN not set.")

    me = _get("me", token, fields="id,username,account_type")
    print(f"[identity] {me.status_code}: {me.text[:200]}")
    if me.status_code != 200:
        sys.exit("Token is invalid or expired — regenerate it.")

    media = _get("me/media", token, fields="id,caption", limit="1")
    data = (media.json() or {}).get("data") or []
    if not data:
        print("[comments] no media on the account yet — cannot probe the comments "
              "edge on a real post. (The next live post's log will confirm it.)")
        return
    mid = data[0]["id"]

    c = _get(f"{mid}/comments", token, fields="id")
    print(f"[comments] GET {mid}/comments -> {c.status_code}: {c.text[:300]}")
    if c.status_code == 200:
        print("RESULT: instagram_business_manage_comments IS granted — the auto "
              "first-comment will post.")
        return
    print("RESULT: the comments edge was refused — instagram_business_manage_comments "
          "is most likely MISSING. Regenerate CK_IG_ACCESS_TOKEN with that scope. "
          "(The reel still publishes; only the first comment is skipped.)")
    sys.exit(2)


if __name__ == "__main__":
    main()
