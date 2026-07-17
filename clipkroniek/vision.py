#!/usr/bin/env python3
"""
Facecam detection via Claude Vision — ONE image call per post.

Reuses ANTHROPIC_API_KEY (no new key/dependency). Given a single frame, it returns
the streamer webcam's normalized bounding box so reformat_reel can STACK the facecam
above the gameplay (a reaction clip's whole appeal) instead of the zoom-crop cutting
it off. Chosen over Gemini precisely because the poster already has the Anthropic key.

Fully non-fatal: no key / no facecam / any error -> None, and the poster falls back
to the standard zoom-crop layout. Cost: ~a fraction of a cent per image.
"""
import os
import json
import base64
import requests

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# Fallback boxes if Claude gives a corner but an implausible box.
_CORNER_BOXES = {
    "top-left": (0.02, 0.04, 0.26, 0.30),
    "top-right": (0.72, 0.04, 0.26, 0.30),
    "bottom-left": (0.02, 0.66, 0.26, 0.30),
    "bottom-right": (0.72, 0.66, 0.26, 0.30),
}


def configured():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _valid_box(b):
    """Accept only plausibly-facecam-sized boxes (guards against false positives that
    would stack a random region). Returns a clamped (x,y,w,h) or None."""
    try:
        x, y, w, h = float(b["x"]), float(b["y"]), float(b["w"]), float(b["h"])
    except Exception:
        return None
    if not (0 <= x < 1 and 0 <= y < 1):
        return None
    if not (0.06 <= w <= 0.55 and 0.06 <= h <= 0.6):   # not the whole frame, not a sliver
        return None
    if x + w > 1.03 or y + h > 1.03:
        return None
    return (round(max(0.0, x), 4), round(max(0.0, y), 4),
            round(min(w, 1 - x), 4), round(min(h, 1 - y), 4))


def detect_facecam(image_path, model=None):
    """Return {'box':(x,y,w,h), 'corner':..., 'confidence':...} if a facecam is found,
    else None."""
    if not configured():
        return None
    model = model or os.environ.get("VISION_MODEL", "claude-sonnet-4-6")
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        prompt = (
            "This is one frame from a 16:9 livestream gaming clip. Streamers usually "
            "overlay a small 'facecam' — a webcam feed of their face/upper body, in a "
            "corner, separate from the gameplay. Decide whether a facecam is present and "
            "where. Return JSON ONLY:\n"
            '{"facecam": true|false, "corner": "top-left|top-right|bottom-left|'
            'bottom-right|other", "box": {"x":0-1,"y":0-1,"w":0-1,"h":0-1}, '
            '"confidence": 0-1}\n'
            "box = the facecam's bounding box as fractions of the frame (x,y is its "
            "top-left). If there is NO facecam (just gameplay/HUD), return "
            '{"facecam": false}. Do NOT treat the game HUD, minimap, kill-feed, or '
            "scoreboard as a facecam — only a real camera feed of a person."
        )
        body = {"model": model, "max_tokens": 200,
                "messages": [{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64",
                                                 "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": prompt}]}]}
        r = requests.post(ANTHROPIC_URL,
                          headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                                   "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"},
                          json=body, timeout=60)
        if r.status_code >= 400:
            print(f"  vision failed {r.status_code}: {r.text[:160]}")
            return None
        text = "".join(b.get("text", "") for b in r.json().get("content", [])
                       if b.get("type") == "text").strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.lstrip().startswith("json"):
                text = text.lstrip()[4:]
            text = text.strip()
        data = json.loads(text)
        if not data.get("facecam"):
            return None
        try:
            if float(data.get("confidence", 1)) < 0.5:
                return None
        except Exception:
            pass
        box = _valid_box(data.get("box") or {})
        if not box:
            box = _CORNER_BOXES.get(data.get("corner"))
        if not box:
            return None
        return {"box": box, "corner": data.get("corner"),
                "confidence": data.get("confidence")}
    except Exception as e:
        print(f"  vision error: {e}")
        return None
