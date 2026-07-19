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
GROQ_TRANSLATE_URL = "https://api.groq.com/openai/v1/audio/translations"

_EN = ("en", "english")


def _is_english(lang):
    return (lang or "").strip().lower() in _EN or (lang or "").strip().lower().startswith("en")


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
                # a list of tuples sends the repeated key -> BOTH word + segment timestamps.
                data=[("model", model), ("response_format", "verbose_json"),
                      ("timestamp_granularities[]", "segment"),
                      ("timestamp_granularities[]", "word")],
                timeout=120)
        if r.status_code >= 400:
            print(f"  groq stt failed {r.status_code}: {r.text[:160]}")
            return None
        j = r.json()
        return _norm(j.get("text"), j.get("segments"), j.get("words"), j.get("language"))
    except Exception as e:
        print(f"  groq stt error: {e}")
        return None


def _groq_translate(wav):
    """Whisper 'translate' task: any language -> ENGLISH, with segment timestamps.
    Used to render an English subtitle line under the original-language captions so a
    western audience can follow a JP/KR clip. Segment-level (word order differs from the
    source, so word-by-word sync would be meaningless). Non-fatal -> None on any failure."""
    model = os.environ.get("GROQ_TRANSLATE_MODEL", "whisper-large-v3")   # turbo has no translate task
    try:
        with open(wav, "rb") as f:
            r = requests.post(
                GROQ_TRANSLATE_URL,
                headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
                files={"file": (os.path.basename(wav), f, "audio/wav")},
                data=[("model", model), ("response_format", "verbose_json")],
                timeout=120)
        if r.status_code >= 400:
            print(f"  groq translate failed {r.status_code}: {r.text[:160]}")
            return None
        segs = [{"text": (s.get("text") or "").strip(),
                 "start": float(s.get("start") or 0.0),
                 "end": float(s.get("end") or 0.0)}
                for s in (r.json().get("segments") or [])]
        return [s for s in segs if s["text"]] or None
    except Exception as e:
        print(f"  groq translate error: {e}")
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
        result = None
        if os.environ.get("GROQ_API_KEY"):
            result = _groq(wav)
            if result is None and cfg.get("self_host", True):
                print("  (groq failed -> self-hosted whisper fallback)")
                result = _faster_whisper(wav, cfg)
        elif cfg.get("self_host_standalone", False):
            result = _faster_whisper(wav, cfg)

        # English translation line for non-English clips (needs Groq's translate task).
        cap = strategy.get("captions", {}) or {}
        if (result and cap.get("translate", True) and os.environ.get("GROQ_API_KEY")
                and not _is_english(result.get("language"))):
            en = _groq_translate(wav)
            if en:
                result["en_segments"] = en
                print(f"  translate: {len(en)} English segments "
                      f"({result.get('language')} -> en)")
        return result
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
