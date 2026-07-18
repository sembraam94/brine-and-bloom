#!/usr/bin/env python3
"""
Test animated captions + streamer credit on ONE real clip.

Downloads CLIP_URL, transcribes, builds the caption ASS, reformats the reel WITH
captions + credit, then hosts a preview mp4 + a couple of still frames to R2 so the
render can be eyeballed. Env: CLIP_URL, GROQ_API_KEY, ANTHROPIC_API_KEY, R2_*.
"""
import os
import sys
import tempfile

import clippost as C
import captions as CAP


def main():
    url = os.environ.get("CLIP_URL")
    if not url:
        sys.exit("Set CLIP_URL.")
    cid = os.environ.get("CLIP_ID") or "captest"
    tmp = tempfile.gettempdir()
    raw = os.path.join(tmp, f"cap_{C._safe_key(cid)}.mp4")

    print(f"Downloading {url}")
    C.download_clip({"id": cid, "url": url, "source": "twitch"}, raw)
    dur = C._probe(raw).get("duration_s")

    strat = {"games": {"gta": "GTA", "valorant": "VALORANT"}, "fg_zoom": {},
             "brand_watermark": True, "min_duration_s": 5, "max_duration_s": 60,
             "smart_trim": {"mode": "always", "post_s": 5, "pre_s": 5,
                            "lead_in_action": 3, "lead_in_min": 3, "lead_in_max": 8,
                            "transcript_window_s": 5},
             "transcribe": {"enabled": True},
             "captions": {"enabled": True, "font_size": 80, "pos_y": 1180}}
    slot = {"game": "valorant", "region": "western"}

    trim, trimmed, meta, tr = C._decide_trim(strat, "captest-forced", raw, dur)
    print(f"trim: {trim} | transcript words: {len(tr.get('words')) if tr else 0} "
          f"| lang: {tr.get('language') if tr else None}")

    reel_max = 60
    if trim:
        reel_dur, off = min(trim[1] - trim[0], reel_max), trim[0]
    else:
        reel_dur, off = (min(float(dur), reel_max) if dur else reel_max), 0.0

    ass = None
    if tr and tr.get("words"):
        name = "ck_captions.ass"
        if CAP.build_ass(tr["words"], os.path.join(os.getcwd(), name), reel_dur,
                         language=tr.get("language"), offset=off):
            ass = name
            print(f"captions: {len(tr['words'])} words -> {name}")
    credit = C._streamer_credit({"url": url})
    print(f"credit: {credit}")

    reel = os.path.join(tmp, "cap_reel.mp4")
    C.reformat_reel(raw, reel, strat, slot, trim=trim, max_s=reel_max,
                    credit=credit, captions_ass=ass)
    rdur = C._probe(reel).get("duration_s")
    print(f"reel built: {C._probe(reel).get('width')}x{C._probe(reel).get('height')} {rdur}s")

    if C.r2_configured():
        for frac in (0.35, 0.55, 0.75):        # a few frames to catch a caption word
            still = os.path.join(tmp, f"cap_still_{int(frac*100)}.jpg")
            if C._extract_frame(reel, (rdur or 4) * frac, still):
                print(f"STILL@{int(frac*100)}%: "
                      + C.host_file_r2(still, f"previews/cap_{C._safe_key(cid)}_{int(frac*100)}.jpg",
                                       "image/jpeg"))
        print("PREVIEW: " + C.host_file_r2(reel, f"previews/cap_{C._safe_key(cid)}.mp4",
                                           "video/mp4"))


if __name__ == "__main__":
    main()
