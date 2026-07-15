#!/usr/bin/env python3
"""
YouTube Shorts upload helper for Clipkroniek — requests-only, no google client libs.

Cross-posts the SAME 9:16 mp4 the poster already builds to the Clipkroniek YouTube
channel as a Short. Self-contained (only needs `requests`), gated on three env vars
so it's a no-op unless the owner has wired it up:

    YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN   (GitHub Actions secrets)

The refresh token is minted once locally via youtube_auth.py; here we just exchange
it for a short-lived access token immediately before each upload.

Two hard-won details (verified against current Google docs, 2026):
  - status.selfDeclaredMadeForKids is ALWAYS sent explicitly (an omitted value can
    leave the video undesignated/hidden until set in Studio).
  - snippet.title is capped at 100 chars and '<' / '>' are rejected (400).
  - A Short is classified automatically by aspect ratio (vertical/square) + length
    (<=180s); our 1080x1920 <=60s reels qualify. '#Shorts' is harmless, not required.
  - An UNAUDITED Cloud project forces privacyStatus=private regardless of request;
    the caller detects that and prints how to unlock public (audit or Studio flip).
"""
import os
import json
import time
import requests

TOKEN_URI = "https://oauth2.googleapis.com/token"
UPLOAD_URI = "https://www.googleapis.com/upload/youtube/v3/videos"
GAMING_CATEGORY = "20"
_RETRYABLE = {500, 502, 503, 504}
_ENV = ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")


def configured():
    """True only if all three YT secrets are present (else cross-posting is skipped)."""
    return all(os.environ.get(k) for k in _ENV)


def get_access_token():
    """Exchange the stored refresh token for a ~1h access token. Refreshed right
    before upload so a large PUT can't straddle expiry. The response carries NO new
    refresh token — the stored one keeps working."""
    r = requests.post(TOKEN_URI, data={
        "client_id": os.environ["YT_CLIENT_ID"],
        "client_secret": os.environ["YT_CLIENT_SECRET"],
        "refresh_token": os.environ["YT_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }, timeout=30)
    if r.status_code == 400 and "invalid_grant" in r.text:
        raise RuntimeError(
            "YouTube invalid_grant: the refresh token is expired or revoked. If the "
            "OAuth consent screen is still in 'Testing', publish it to Production and "
            "re-mint the token (sensitive scopes expire after 7 days in Testing).")
    r.raise_for_status()
    return r.json()["access_token"]


def _sanitize_title(raw):
    # <=100 chars; '<' and '>' are disallowed by the API and 400 the request.
    return (raw or "").replace("<", "").replace(">", "").strip()[:100] or "Gaming clip"


def upload_short(file_path, *, title, description="", tags=None,
                 category_id=GAMING_CATEGORY, privacy="public",
                 made_for_kids=False, max_attempts=4):
    """Resumable-upload an mp4 as a Short. Returns the video resource dict (has
    'id' and 'status.privacyStatus'). Raises on unrecoverable failure — callers
    treat cross-posting as non-fatal."""
    token = get_access_token()
    meta = {
        "snippet": {
            "title": _sanitize_title(title),
            "description": (description or "").encode("utf-8")[:4900].decode("utf-8", "ignore"),
            "tags": [t for t in (tags or []) if t][:15],
            "categoryId": str(category_id),
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": bool(made_for_kids),
        },
    }
    size = os.path.getsize(file_path)
    last = ""
    for attempt in range(1, max_attempts + 1):
        # (Re)initiate a resumable session. For 15-45MB files a single PUT is
        # reliable, so on failure we just re-initiate and re-send rather than
        # byte-range resuming (simpler, fewer footguns).
        init = requests.post(
            UPLOAD_URI,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": "video/*",
            },
            data=json.dumps(meta).encode("utf-8"),
            timeout=60,
        )
        if init.status_code >= 400:
            # 401 -> token went stale between refresh and now; get a fresh one and retry.
            if init.status_code == 401 and attempt < max_attempts:
                token = get_access_token()
                continue
            raise RuntimeError(f"YouTube initiate failed {init.status_code}: {init.text[:300]}")
        session = init.headers.get("Location")
        if not session:
            raise RuntimeError("YouTube initiate returned no resumable session URL.")

        try:
            with open(file_path, "rb") as f:
                put = requests.put(
                    session,
                    headers={"Content-Type": "video/*", "Content-Length": str(size)},
                    data=f, timeout=600)
        except requests.RequestException as e:
            last = str(e)
            if attempt < max_attempts:
                time.sleep(2 ** attempt)
                continue
            raise

        if put.status_code in (200, 201):
            return put.json()
        last = f"{put.status_code}: {put.text[:300]}"
        if put.status_code in _RETRYABLE and attempt < max_attempts:
            time.sleep(2 ** attempt)
            continue
        # 4xx (e.g. 400 bad title) is not retryable — fail loudly.
        raise RuntimeError(f"YouTube upload failed {last}")
    raise RuntimeError(f"YouTube upload failed after {max_attempts} attempts: {last}")
