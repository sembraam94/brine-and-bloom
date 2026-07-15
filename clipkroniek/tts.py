#!/usr/bin/env python3
"""
Optional AI voiceover for the long-form compilation.

Gated on REPLICATE_API_TOKEN; self-contained (requests only). Runs a Replicate TTS
model and downloads the audio. Non-fatal: any failure returns None and the caller
falls back to silent text cards.

Default model is Kokoro (cheap + clear); override with TTS_MODEL (a Replicate
"owner/name" slug) and per-model params via TTS_VOICE / the `extra` arg.
"""
import os
import time
import requests

REPLICATE = "https://api.replicate.com/v1"
_DONE = {"succeeded", "failed", "canceled"}


def configured():
    return bool(os.environ.get("REPLICATE_API_TOKEN"))


def synthesize(text, out_path, *, model=None, extra=None, timeout=300):
    """TTS `text` -> out_path (audio file). Returns out_path or None.
    Uses the model-endpoint predictions API (auto-resolves the latest version) with
    Prefer: wait, then polls."""
    if not configured() or not (text or "").strip():
        return None
    token = os.environ["REPLICATE_API_TOKEN"]
    model = model or os.environ.get("TTS_MODEL", "jaaari/kokoro-82m")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    inp = {"text": text}
    voice = os.environ.get("TTS_VOICE")
    if voice:
        inp["voice"] = voice
    if extra:
        inp.update(extra)
    try:
        r = requests.post(f"{REPLICATE}/models/{model}/predictions",
                          headers={**headers, "Prefer": "wait"},
                          json={"input": inp}, timeout=90)
        if r.status_code >= 400:
            print(f"  tts create failed {r.status_code}: {r.text[:200]}")
            return None
        pred = r.json()
        waited = 0
        while pred.get("status") not in _DONE and waited < timeout:
            time.sleep(3)
            waited += 3
            g = requests.get(f"{REPLICATE}/predictions/{pred['id']}",
                             headers=headers, timeout=30)
            g.raise_for_status()
            pred = g.json()
        if pred.get("status") != "succeeded":
            print(f"  tts did not succeed: {pred.get('status')} "
                  f"{str(pred.get('error'))[:150]}")
            return None
        out = pred.get("output")
        url = out[0] if isinstance(out, list) and out else out
        if not isinstance(url, str) or not url.startswith("http"):
            print(f"  tts: unexpected output shape: {str(out)[:120]}")
            return None
        audio = requests.get(url, timeout=180)
        audio.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(audio.content)
        return out_path
    except Exception as e:
        print(f"  tts error: {e}")
        return None
