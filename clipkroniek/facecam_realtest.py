#!/usr/bin/env python3
"""
Run ONE specific clip through facecam detection + reformat and host a PRIVATE preview.

For eyeballing the facecam-stack on real footage (e.g. yesterday's post). No IG/YT.
Env: CLIP_URL (required), CLIP_ID (optional, for the filename), ANTHROPIC_API_KEY,
R2_*.  Prints whether a facecam was detected and the preview URL.
"""
import os
import sys
import tempfile

import clippost as C
import vision


def main():
    url = os.environ.get("CLIP_URL")
    if not url:
        sys.exit("Set CLIP_URL to a Twitch clip URL.")
    cid = os.environ.get("CLIP_ID") or "realtest"
    tmp = tempfile.gettempdir()
    raw = os.path.join(tmp, f"fcr_{C._safe_key(cid)}.mp4")

    print(f"Downloading {url}")
    C.download_clip({"id": cid, "url": url, "source": "twitch"}, raw)
    dur = C._probe(raw).get("duration_s")

    box = None
    frame = os.path.join(tmp, "fcr_frame.jpg")
    if C._extract_frame(raw, (dur or 3) * 0.4, frame):
        fc = vision.detect_facecam(frame)
        if fc:
            box = fc["box"]
            print(f"FACECAM DETECTED: {fc.get('corner')} box={box} "
                  f"conf={fc.get('confidence')} -> STACKED layout")
        else:
            print("No facecam detected -> standard layout")
    else:
        print("(frame extract failed — standard layout)")

    strat = {"games": {"gta": "GTA", "valorant": "VALORANT"}, "fg_zoom": {},
             "brand_watermark": True, "smart_trim": {"mode": "off"}}
    slot = {"game": "gta", "region": "western"}
    reel = os.path.join(tmp, "fcr_reel.mp4")
    C.reformat_reel(raw, reel, strat, slot, facecam=box, max_s=60)
    pr = C._probe(reel)
    print(f"Reel: {pr.get('width')}x{pr.get('height')} {pr.get('duration_s')}s "
          f"({os.path.getsize(reel) // 1024} KB)")

    if C.r2_configured():
        key = f"previews/facecam_real_{C._safe_key(cid)}.mp4"
        print("PREVIEW: " + C.host_file_r2(reel, key, "video/mp4"))
        still = os.path.join(tmp, "fcr_still.jpg")
        if C._extract_frame(reel, (pr.get("duration_s") or 4) * 0.5, still):
            print("STILL: " + C.host_file_r2(
                still, f"previews/facecam_real_{C._safe_key(cid)}.jpg", "image/jpeg"))
    print("RESULT: " + ("STACKED (facecam)" if box else "standard layout"))


if __name__ == "__main__":
    main()
