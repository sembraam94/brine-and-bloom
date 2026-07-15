#!/usr/bin/env python3
"""
Optional Gemini clip analysis — grounds the long-form voiceover in what actually
happens in a clip (instead of guessing from metadata).

Gated on GEMINI_API_KEY; self-contained (requests only). Sends a SHORT trimmed mp4
inline to Gemini's generateContent and returns a one-sentence description of the
moment. Supports high-fps sampling for fast gameplay. Fully non-fatal: any failure
returns None and the caller falls back to generic narration.

Cost is a fraction of a cent per short clip (see clipkroniek/CLAUDE.md).
"""
import os
import base64
import requests

API = "https://generativelanguage.googleapis.com/v1beta/models"
# Inline request bodies must stay well under the ~20MB request cap — pass a TRIMMED
# segment (a few seconds), not the whole source clip.
MAX_INLINE_BYTES = 18_000_000


def configured():
    return bool(os.environ.get("GEMINI_API_KEY"))


def analyze_clip(path, *, hint="", model=None, fps=None):
    """Return a one-sentence description of the clip's key moment, or None.
    `fps` samples the video (higher = better on fast gameplay; None = model default
    ~1fps). `hint` (e.g. the game + clip title) steers it without letting it invent."""
    if not configured():
        return None
    model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    try:
        with open(path, "rb") as f:
            data = f.read()
        if len(data) > MAX_INLINE_BYTES:
            print(f"  gemini: segment too big to inline ({len(data)//1_000_000}MB) — skipping")
            return None
        prompt = (
            "You are watching a short gaming clip. In ONE sentence (max 16 words), "
            "describe the single most exciting thing that happens — the play, the "
            "fail, the chaos, or the reaction. Be concrete but do NOT invent specifics "
            "you cannot clearly see (exact kill counts, names). "
            + (f"Context: {hint}. " if hint else "")
            + "Reply with only the sentence, no preamble."
        )
        video_part = {"inline_data": {"mime_type": "video/mp4",
                                      "data": base64.b64encode(data).decode()}}
        if fps:
            video_part["video_metadata"] = {"fps": int(fps)}
        body = {
            "contents": [{"parts": [video_part, {"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 60},
        }
        r = requests.post(f"{API}/{model}:generateContent",
                          params={"key": os.environ["GEMINI_API_KEY"]},
                          json=body, timeout=180)
        if r.status_code >= 400:
            print(f"  gemini analyze failed {r.status_code}: {r.text[:200]}")
            return None
        cands = r.json().get("candidates") or []
        if not cands:
            return None
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = " ".join(p.get("text", "") for p in parts).strip()
        # tidy: single line, drop wrapping quotes
        text = " ".join(text.split()).strip('"').strip()
        return text or None
    except Exception as e:
        print(f"  gemini analyze error: {e}")
        return None
