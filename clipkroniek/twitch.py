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


def find_game(name, client_id, token, http):
    """Resolve a category by name WITHOUT exiting. Tries the exact-name lookup,
    then a fuzzy category search (handles near-miss names like 'Counter-Strike 2'
    -> 'Counter-Strike'). Returns (game_id, canonical_name) or None."""
    try:
        r = http("GET", f"{HELIX}/games", params={"name": name},
                 headers=_headers(client_id, token), timeout=30)
        r.raise_for_status()
        data = r.json().get("data", [])
        if data:
            return data[0]["id"], data[0]["name"]
    except Exception:
        pass
    try:
        r = http("GET", f"{HELIX}/search/categories",
                 params={"query": name, "first": 5},
                 headers=_headers(client_id, token), timeout=30)
        r.raise_for_status()
        data = r.json().get("data", [])
        low = (name or "").strip().lower()
        for d in data:                       # prefer an exact (case-insensitive) hit
            if (d.get("name") or "").strip().lower() == low:
                return d["id"], d["name"]
        if data:
            return data[0]["id"], data[0]["name"]
    except Exception:
        pass
    return None


def get_top_games(client_id, token, http, first=20):
    """The top games right now by viewership (for the platform-wide sweep).
    Returns [{id, name, ...}]."""
    r = http("GET", f"{HELIX}/games/top", params={"first": min(int(first), 100)},
             headers=_headers(client_id, token), timeout=30)
    r.raise_for_status()
    return r.json().get("data", [])


def get_streams_by_language(language, client_id, token, http, first=100):
    """LIVE streams in a given language, ordered by viewers. This is the only reliable way
    to find a language niche: clip search is ranked by view count, so a small-language clip
    never surfaces in a big game's top pages (Just Chatting's top 285 clips contained zero
    'nl'). Discover the language's broadcasters here, then pull THEIR clips."""
    out, cursor = [], None
    while len(out) < first:
        params = {"language": language, "first": min(100, first - len(out))}
        if cursor:
            params["after"] = cursor
        r = http("GET", f"{HELIX}/streams", params=params,
                 headers=_headers(client_id, token), timeout=30)
        r.raise_for_status()
        j = r.json()
        data = j.get("data") or []
        out.extend(data)
        cursor = (j.get("pagination") or {}).get("cursor")
        if not data or not cursor:
            break
    return out[:first]


def get_clips_by_id(ids, client_id, token, http):
    """Current data (incl. view_count) for specific clip ids, up to 100 per request.
    Returns {id: clip_dict}."""
    out = {}
    ids = [i for i in ids if i]
    for i in range(0, len(ids), 100):
        params = [("id", c) for c in ids[i:i + 100]]
        r = http("GET", f"{HELIX}/clips", params=params,
                 headers=_headers(client_id, token), timeout=30)
        r.raise_for_status()
        for c in r.json().get("data", []):
            out[c["id"]] = c
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
