#!/usr/bin/env python3
"""
Synthetic smoke test for the facecam-STACK reel path.

Builds a fake 16:9 clip (gameplay colour + a bright red "facecam" box in the
bottom-right + silent audio), then runs reformat_reel with a hardcoded facecam box
matching it — confirming the stacked ffmpeg graph executes and yields a valid
1080x1920 reel. No IG/YouTube. Hosts a preview to R2 so the layout can be eyeballed.

Run via the 'Clipkroniek — facecam stack test' workflow.
"""
import os
import sys
import subprocess
import tempfile

import clippost as C


def main():
    C._ensure_tool("ffmpeg")
    tmp = tempfile.gettempdir()
    src = os.path.join(tmp, "fc_src.mp4")

    # 6s 1920x1080: dark-blue "gameplay" + a red box bottom-right = fake facecam.
    cmd = ["ffmpeg", "-y",
           "-f", "lavfi", "-i", "color=c=0x123456:s=1920x1080:d=6",
           "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-shortest", "-t", "6",
           "-vf", "drawbox=x=1440:y=760:w=440:h=280:color=red@0.95:t=fill",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", src]
    if subprocess.run(cmd, capture_output=True, text=True).returncode != 0 or not os.path.exists(src):
        sys.exit("failed to build synthetic source clip")
    print("synthetic source built (1920x1080, red facecam box bottom-right)")

    strat = {"games": {"gta": "GTA"}, "fg_zoom": {}, "brand_watermark": True,
             "smart_trim": {"mode": "off"}}
    slot = {"game": "gta", "region": "western"}
    reel = os.path.join(tmp, "fc_reel.mp4")
    # box matches the drawn red box: x=1440/1920, y=760/1080, w=440/1920, h=280/1080
    C.reformat_reel(src, reel, strat, slot, facecam=(0.75, 0.7037, 0.2292, 0.2593))

    pr = C._probe(reel)
    print(f"STACKED reel built: {pr.get('width')}x{pr.get('height')} "
          f"{pr.get('duration_s')}s ({os.path.getsize(reel) // 1024} KB)")
    if not (pr.get("width") == 1080 and pr.get("height") == 1920):
        sys.exit(f"reel is not 1080x1920: {pr.get('width')}x{pr.get('height')}")
    if C.r2_configured():
        print("Preview (eyeball the stacked layout): "
              + C.host_file_r2(reel, "previews/facecam_test.mp4", "video/mp4"))
    print("OK — facecam stacked build works end-to-end.")


if __name__ == "__main__":
    main()
