#!/usr/bin/env python3
"""
News/momentum scanner for Clipkroniek's adaptive game rotation.

Uses Claude with the web-search server tool to find which games are trending or
have a recent/imminent launch and would make strong short-form CLIP content for a
Western audience. Returns candidate games with their exact Twitch category names,
a momentum score, and a release flag (launch waves = free reach).

Self-contained: caller passes the Anthropic key and an http() callable.
Degrades gracefully — any failure returns [] and the caller simply doesn't rotate.
"""

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
# Sonnet 4.6 supports the dynamic-filtering web-search tool (no beta header).
SCAN_MODEL = "claude-sonnet-4-6"
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 6}

_HEADERS = {
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
}


def _extract_text(resp_json):
    return "".join(b.get("text", "") for b in (resp_json.get("content") or [])
                   if b.get("type") == "text").strip()


def _parse_json(text):
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
        text = text.strip()
    try:
        import json
        return json.loads(text)
    except Exception:
        import json
        i, j = text.find("{"), text.rfind("}")
        if i >= 0 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except Exception:
                return None
    return None


def scan_games(anthropic_key, http, active_names, benched_names):
    """Return a list of candidate dicts:
        {key, twitch_name, momentum(0-100), is_new_release(bool),
         release_window("imminent"|"recent"|"none"), region_lean, why}
    Empty list on any failure."""
    system = (
        "You are the game scout for 'Clipkroniek', an Instagram page that reposts "
        "short, high-energy gaming CLIPS (insane plays, funny fails, chaos, rage) "
        "for a Western/English-first audience. Use web search to find which games "
        "RIGHT NOW would produce the best clip content. Prioritise: (a) games with a "
        "RECENT or IMMINENT major launch or update (launch waves give brand-new clip "
        "pages huge free reach), and (b) games surging in viewership/buzz that are "
        "highly 'clippable' — FPS, battle royale, extraction shooters, fighting "
        "games, chaotic multiplayer, popular streamer games. AVOID slow/cinematic "
        "single-player games (poor clip fodder).\n"
        "Return ONLY a JSON object (no prose, no markdown):\n"
        '{"candidates": [{"key": "short_snake_case", "twitch_name": "exact Twitch '
        'category name", "momentum": 0-100, "is_new_release": true|false, '
        '"release_window": "imminent"|"recent"|"none", "region_lean": '
        '"western"|"asian"|"global", "why": "one short line"}], "notes": "one line"}\n'
        "- twitch_name MUST be the exact Twitch directory/category name (e.g. "
        "'Grand Theft Auto V', 'VALORANT', 'Call of Duty: Warzone', 'Fortnite', "
        "'Marvel Rivals', 'Apex Legends', 'Counter-Strike 2', 'Battlefield 6').\n"
        "- Return 5-8 candidates, best first. It's fine to include a currently-active "
        "game if it still has strong momentum (that signals it should be kept)."
    )
    user = (
        "Currently active games: " + (", ".join(active_names) or "(none)") + ".\n"
        "Previously tried, now benched (can return if momentum is back): "
        + (", ".join(benched_names) or "(none)") + ".\n"
        "Find the best games to post clips from this week."
    )
    body = {
        "model": SCAN_MODEL,
        "max_tokens": 2000,
        "system": system,
        "tools": [WEB_SEARCH_TOOL],
        "messages": [{"role": "user", "content": user}],
    }
    headers = dict(_HEADERS, **{"x-api-key": anthropic_key})
    try:
        resp = http("POST", ANTHROPIC_URL, headers=headers, json=body, timeout=180)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  news scan request failed: {e}")
        return []

    # The server-tool loop can pause after ~10 iterations; resume once.
    if data.get("stop_reason") == "pause_turn":
        try:
            body2 = dict(body)
            body2["messages"] = [
                {"role": "user", "content": user},
                {"role": "assistant", "content": data.get("content", [])},
            ]
            resp = http("POST", ANTHROPIC_URL, headers=headers, json=body2, timeout=180)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  news scan resume failed: {e}")

    parsed = _parse_json(_extract_text(data))
    if not isinstance(parsed, dict):
        return []
    cands = parsed.get("candidates")
    if parsed.get("notes"):
        print(f"  news scan notes: {str(parsed.get('notes'))[:120]}")
    return cands if isinstance(cands, list) else []
