#!/usr/bin/env python3
"""
Test clip transcription + cut-moment assist on ONE specific clip.

Downloads CLIP_URL, transcribes it (Groq -> self-host fallback), and runs the full
_decide_trim (smart_trim forced 'always') so you can see: the transcript, the audio
peak, the transcript's verbal-reaction moment, and the final blended cut. No IG/YT.
Env: CLIP_URL, GROQ_API_KEY, ANTHROPIC_API_KEY.
"""
import os
import sys
import json
import tempfile

import clippost as C
import transcribe


def main():
    url = os.environ.get("CLIP_URL")
    if not url:
        sys.exit("Set CLIP_URL to a Twitch clip URL.")
    cid = os.environ.get("CLIP_ID") or "stttest"
    tmp = tempfile.gettempdir()
    raw = os.path.join(tmp, f"stt_{C._safe_key(cid)}.mp4")

    print(f"Downloading {url}")
    C.download_clip({"id": cid, "url": url, "source": "twitch"}, raw)
    dur = C._probe(raw).get("duration_s")
    print(f"Clip duration: {dur}s")

    strat = {"smart_trim": {"mode": "always", "pre_s": 7, "post_s": 5,
                            "transcript_window_s": 5},
             "min_duration_s": 5,
             "transcribe": {"enabled": True, "self_host": True,
                            "self_host_standalone": False,
                            "self_host_model": "large-v3-turbo"}}
    print(f"transcribe.available: {transcribe.available(strat)}")

    tr = transcribe.transcribe(raw, strat)
    if tr:
        txt = (tr.get("text") or "").strip()
        print(f"Language: {tr.get('language')}  | segments: {len(tr.get('segments') or [])}")
        print("Transcript: " + (txt[:500] + ("..." if len(txt) > 500 else "") or "(empty)"))
    else:
        print("No transcript (no speech / STT unavailable).")

    win, applied, meta, _tr = C._decide_trim(strat, "stttest-forced", raw, dur)
    print("meta: " + json.dumps(meta))
    print(f"RESULT: cut center={win[2] if win else None}s "
          f"(window {win[0]}-{win[1]}s) | audio_peak={meta.get('audio_peak')}s | "
          f"verbal_moment={meta.get('verbal_moment')}s | "
          f"transcript_used={meta.get('transcript_used')}")


if __name__ == "__main__":
    main()
