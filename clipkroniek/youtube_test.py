#!/usr/bin/env python3
"""
One-off YouTube upload smoke test.

Generates a tiny 1080x1920 test clip and uploads it as PRIVATE to confirm the YT_*
secrets + youtube.py work end-to-end — WITHOUT touching Instagram and without making
anything public. Delete the test video from YouTube Studio afterward.

Run via the 'Clipkroniek — YouTube upload test' workflow (uses the YT_* secrets).
"""
import os
import sys
import tempfile
import subprocess

import youtube
from clippost import _ensure_tool


def main():
    if not youtube.configured():
        sys.exit("YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN are not all set.")

    _ensure_tool("ffmpeg")
    out = os.path.join(tempfile.gettempdir(), "ck_yt_smoketest.mp4")
    # 3s solid-colour 1080x1920 clip + silent audio track = a valid vertical Short.
    cmd = ["ffmpeg", "-y",
           "-f", "lavfi", "-i", "color=c=0x1E88E5:s=1080x1920:d=3",
           "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
           "-shortest", "-t", "3",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", out]
    print("Generating a 3s test clip...")
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0 or not os.path.exists(out):
        sys.exit("ffmpeg failed:\n" + (p.stderr or "")[-1000:])

    print("Uploading to YouTube as PRIVATE...")
    vid = youtube.upload_short(
        out,
        title="Clipkroniek upload test — safe to delete",
        description="Automated smoke test of the auto-poster's YouTube upload. Private; delete me.",
        tags=["test"],
        privacy="private",
    )
    vid_id = vid.get("id")
    status = (vid.get("status") or {}).get("privacyStatus")
    print(f"\nSUCCESS ✅  https://youtu.be/{vid_id}  (privacy={status})")
    print("It's a PRIVATE test video (only you can see it) — delete it in YouTube Studio.")
    print("The real daily poster requests 'public'; its first live run will log whether "
          "your project's audit status lets it post public or forces private.")


if __name__ == "__main__":
    main()
