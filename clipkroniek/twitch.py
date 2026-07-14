#!/usr/bin/env python3
"""
Twitch Helix helpers for Clipkroniek clip discovery.

Self-contained (only needs `requests` via the injected `http` callable). Uses an
app access token (client-credentials) — no user login, read-only public data.

Docs: Get Clips returns, per clip: id, url, embed_url, broadcaster_id/name,
creator_id/name, video_id, game_id, language, title, view_count, created_at,
duration, thumbnail_url. We constrain to a game + a time window (last 24h) and
rank by view_count ourselves (the API doesn't sort by views).
"""
import sys

_TOKEN_CACHE = {}

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX = "https://api.twitch.tv/helix"


def get_app_token(client_id, client_secret, http):
    """client-credentials app token (cached for the process)."""
    if _TOKEN_CACHE.get("token"):
        return _TOKEN_CACHE["token"]
    r = http("POST", TOKEN_URL, params={
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    }, timeout=30)
    r.raise_for_status()
    tok = r.json().get("access_token")
    if not tok:
        sys.exit(f"Could not get Twitch app token: {r.text[:300]}")
    _TOKEN_CACHE["token"] = tok
    return tok


def _headers(client_id, token):
    return {"Client-Id": client_id, "Authorization": f"Bearer {token}"}


def resolve_game_id(name, client_id, token, http):
    """Look up a game's Twitch id by exact name (e.g. 'Grand Theft Auto V')."""
    r = http("GET", f"{HELIX}/games", params={"name": name},
             headers=_headers(client_id, token), timeout=30)
    r.raise_for_status()
    data = r.json().get("data", [])
    if not data:
        sys.exit(f"Twitch game not found: {name!r}")
    return data[0]["id"]


def get_recent_clips(game_id, started_at, ended_at, client_id, token, http,
                     pages=4, page_size=100):
    """All clips for a game created within [started_at, ended_at] (RFC3339 Z).
    Paginates up to `pages` * `page_size`. Returns the raw clip dicts."""
    out, cursor = [], None
    for _ in range(pages):
        params = {
            "game_id": game_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "first": page_size,
        }
        if cursor:
            params["after"] = cursor
        r = http("GET", f"{HELIX}/clips", params=params,
                 headers=_headers(client_id, token), timeout=30)
        r.raise_for_status()
        j = r.json()
        out.extend(j.get("data", []))
        cursor = (j.get("pagination") or {}).get("cursor")
        if not cursor:
            break
    return out


def resolve_user_ids(logins, client_id, token, http):
    """Map streamer logins -> user ids (batched, up to 100 per request)."""
    out = {}
    logins = [l.strip().lower() for l in logins if l and l.strip()]
    for i in range(0, len(logins), 100):
        chunk = logins[i:i + 100]
        params = [("login", l) for l in chunk]
        r = http("GET", f"{HELIX}/users", params=params,
                 headers=_headers(client_id, token), timeout=30)
        r.raise_for_status()
        for u in r.json().get("data", []):
            out[u["login"].lower()] = u["id"]
    return out


def get_broadcaster_clips(broadcaster_id, started_at, ended_at, client_id, token,
                          http, first=50):
    """A single broadcaster's clips in [started_at, ended_at]. NOTE: returns clips
    for ANY game the broadcaster played — filter by clip['game_id'] downstream."""
    r = http("GET", f"{HELIX}/clips",
             params={"broadcaster_id": broadcaster_id, "started_at": started_at,
                     "ended_at": ended_at, "first": first},
             headers=_headers(client_id, token), timeout=30)
    r.raise_for_status()
    return r.json().get("data", [])
