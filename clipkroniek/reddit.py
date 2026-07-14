#!/usr/bin/env python3
"""
Reddit discovery for Clipkroniek — community-upvoted "top of the day" video clips
from gaming subreddits. Curation by upvotes tends to beat Twitch's raw view sort.

App-only OAuth (client-credentials) — read-only public data, no user login.
Returns clip dicts in the SAME shape as twitch.py so the rest of the pipeline
(download via yt-dlp on the post URL, caption, publish) is source-agnostic.

Reddit requires a descriptive, unique User-Agent or it rate-limits hard.
"""
import sys

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
OAUTH = "https://oauth.reddit.com"
_TOKEN_CACHE = {}


def get_app_token(client_id, client_secret, user_agent, http):
    """Application-only token (cached). Works for 'script'/'web' (confidential) apps."""
    if _TOKEN_CACHE.get("token"):
        return _TOKEN_CACHE["token"]
    r = http("POST", TOKEN_URL,
             data={"grant_type": "client_credentials"},
             auth=(client_id, client_secret),
             headers={"User-Agent": user_agent}, timeout=30)
    r.raise_for_status()
    tok = r.json().get("access_token")
    if not tok:
        sys.exit(f"Reddit app token failed: {r.text[:200]}")
    _TOKEN_CACHE["token"] = tok
    return tok


def _headers(token, user_agent):
    return {"Authorization": f"bearer {token}", "User-Agent": user_agent}


def get_top_video_posts(subreddits, token, user_agent, http,
                        min_d=5.0, max_d=60.0, cutoff_ts=0, limit=50):
    """Top-of-day self-hosted (v.redd.it) video posts across the given subreddits,
    filtered to SFW, in-window, right length. Returns unified clip dicts."""
    out = []
    for sub in subreddits:
        r = http("GET", f"{OAUTH}/r/{sub}/top",
                 params={"t": "day", "limit": limit},
                 headers=_headers(token, user_agent), timeout=30)
        if r.status_code != 200:
            print(f"  reddit r/{sub}: HTTP {r.status_code}")
            continue
        for child in r.json().get("data", {}).get("children", []):
            d = child.get("data", {})
            if d.get("over_18") or d.get("stickied"):
                continue
            if not d.get("is_video"):
                continue
            rv = ((d.get("secure_media") or {}).get("reddit_video")
                  or (d.get("media") or {}).get("reddit_video"))
            if not rv:
                continue
            if float(d.get("created_utc") or 0) < cutoff_ts:
                continue
            dur = rv.get("duration")
            if dur is not None and (dur < min_d or dur > max_d):
                continue
            out.append({
                "id": d.get("id"),
                "url": "https://www.reddit.com" + (d.get("permalink") or ""),
                "title": d.get("title"),
                "view_count": int(d.get("score") or 0),   # upvotes = the ranking proxy
                "language": "en",
                "broadcaster_name": f"r/{sub}",
                "creator_name": f"u/{d.get('author')}",
                "duration": dur,
                "source": "reddit",
                "subreddit": sub,
                "author": d.get("author"),
            })
    return out
