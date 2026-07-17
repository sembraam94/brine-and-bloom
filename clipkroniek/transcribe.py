#!/usr/bin/env python3
"""
Speech-to-text for clips — Groq (primary, effectively free) + self-hosted
faster-whisper (fallback, $0 on the CI runner).

Used as a HELPER for the cut-moment decision: the transcript's timestamps reveal
where the verbal reaction lands, which nudges the smart-trim window — but the audio
peak still leads, and a clip with no speech just falls back to audio-only.

Gating (matches "Groq primary, self-host backup"):
  - GROQ_API_KEY set  -> Groq; if Groq fails and transcribe.self_host is true,
    fall back to self-hosted faster-whisper.
  - GROQ_API_KEY absent -> transcription is OFF (audio-peak only) UNLESS
    transcribe.self_host_standalone is true (opt-in to run whisper on CI without Groq).
This keeps the heavy self-host path from firing every run before Groq is configured.
Non-fatal throughout: any failure returns None.
"""
import os
import subprocess

import requests

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def available(strategy):
    cfg = strategy.get("transcribe", {}) or {}
    if not cfg.get("enabled", True):
        return False
    if os.environ.get("GROQ_API_KEY"):
        return True
    return bool(cfg.get("self_host_standalone", False))


def _extract_wav(video_path):
    from clippost import _ensure_tool          # lazy import (avoids a cycle at import)
    _ensure_tool("ffmpeg")
    wav = video_path + ".stt.wav"
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
         "-f", "wav", wav], timeout=120, stderr=subprocess.PIPE)
    return wav if (proc.returncode == 0 and os.path.exists(wav)) else None


def _norm(text, segments, words, language):
    return {
        "text": (text or "").strip(),
        "segments": [{"text": (s.get("text") or "").strip(),
                      "start": float(s.get("start") or 0.0),
                      "end": float(s.get("end") or 0.0)} for s in (segments or [])],
        "words": [{"word": w.get("word"), "start": float(w.get("start") or 0.0),
                   "end": float(w.get("end") or 0.0)} for w in (words or [])],
        "language": language,
    }


def _groq(wav):
    model = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")
    try:
        with open(wav, "rb") as f:
            r = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
                files={"file": (os.path.basename(wav), f, "audio/wav")},
                data={"model": model, "response_format": "verbose_json",
                      "timestamp_granularities[]": "word"},
                timeout=120)
        if r.status_code >= 400:
            print(f"  groq stt failed {r.status_code}: {r.text[:160]}")
            return None
        j = r.json()
        return _norm(j.get("text"), j.get("segments"), j.get("words"), j.get("language"))
    except Exception as e:
        print(f"  groq stt error: {e}")
        return None


def _faster_whisper(wav, cfg):
    try:
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            import sys
            print("  installing faster-whisper (self-hosted STT fallback)...")
            subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                            "faster-whisper"], check=True)
            from faster_whisper import WhisperModel
        name = os.environ.get("STT_MODEL") or cfg.get("self_host_model", "large-v3-turbo")
        model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=4)
        segs, info = model.transcribe(wav, vad_filter=True, word_timestamps=True,
                                      condition_on_previous_text=False)
        segments, words, texts = [], [], []
        for s in segs:
            segments.append({"text": s.text, "start": s.start, "end": s.end})
            texts.append(s.text or "")
            for w in (s.words or []):
                words.append({"word": w.word, "start": w.start, "end": w.end})
        return _norm(" ".join(texts), segments, words, info.language)
    except Exception as e:
        print(f"  faster-whisper error: {e}")
        return None


def transcribe(video_path, strategy):
    """Return {text, segments, words, language} or None."""
    cfg = strategy.get("transcribe", {}) or {}
    if not cfg.get("enabled", True):
        return None
    wav = _extract_wav(video_path)
    if not wav:
        return None
    try:
        if os.environ.get("GROQ_API_KEY"):
            result = _groq(wav)
            if result is None and cfg.get("self_host", True):
                print("  (groq failed -> self-hosted whisper fallback)")
                result = _faster_whisper(wav, cfg)
            return result
        if cfg.get("self_host_standalone", False):
            return _faster_whisper(wav, cfg)
        return None
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
