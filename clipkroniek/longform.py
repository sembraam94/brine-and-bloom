#!/usr/bin/env python3
"""
Weekly LONG-FORM best-of compilation (YouTube only) — dormant until enabled.

A ranked countdown of the week's best clips assembled into one 16:9 long-form video:
  intro card -> [ #N card -> clip ] (worst -> best, #1 revealed last) -> outro card.

Optional, stacking layers (each degrades gracefully if its key/flag is absent):
  - voiceover (strategy.longform.voiceover + REPLICATE_API_TOKEN): AI narration over
    each card via tts.py; card length = narration length. Clips keep their own audio.
  - gemini_grounding (strategy.longform.gemini_grounding + GEMINI_API_KEY): Gemini
    watches each trimmed clip (high-fps) so the narration is specific, not generic.

Off by default (strategy.longform.enabled=false) — flip it + add the keys to go live.
Triggered by FORMAT_OVERRIDE=longform (weekly cron or manual dispatch).

Long-form is YouTube-only: a ~10-min 16:9 video can't be an IG Reel, and long-form
is where watch-time / durable YouTube growth actually lives.
"""
import os
import tempfile

import clippost as C
import youtube
import gemini
import tts

LW, LH = 1920, 1080          # 16:9 long-form canvas
_ENC = ["-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2"]


def _run_ffmpeg(cmd, what):
    import subprocess
    proc = subprocess.run(cmd, timeout=600, stderr=subprocess.PIPE)
    if proc.returncode != 0 or not os.path.exists(cmd[-1]):
        raise RuntimeError(f"ffmpeg {what} failed:\n"
                           + proc.stderr.decode("utf-8", "replace")[-1200:])
    return cmd[-1]


def _build_segment(raw, out, rank, streamer, seg_max):
    """A single clip as a 16:9 segment: trimmed to the action peak, scaled+padded to
    1920x1080, with a big #rank corner tag + streamer credit, loudness-normalised."""
    C._ensure_tool("ffmpeg")
    dur = C._probe(raw).get("duration_s")
    half = max(2.0, seg_max / 2.0)
    win = C._audio_peak_window(raw, dur, half, half, min(6.0, seg_max))
    seek = ["-ss", f"{win[0]:.2f}"] if win else []
    seg = min((win[1] - win[0]) if win else (dur or seg_max), float(seg_max))
    font = C._ensure_font(bold=True)

    vf = (f"[0:v]scale={LW}:{LH}:force_original_aspect_ratio=decrease,"
          f"pad={LW}:{LH}:(ow-iw)/2:(oh-ih)/2:color=black,fps=30[v0]")
    label = "[v0]"
    draws = []
    if font:
        draws.append(C._drawtext(font, f"#{rank}", fontcolor="yellow", fontsize=110,
                                 borderw=7, bordercolor="black", x=64, y=54))
        if streamer:
            draws.append(C._drawtext(font, C._clean_drawtext(streamer), fontcolor="white",
                                     fontsize=48, borderw=4, bordercolor="black@0.85",
                                     x=64, y="h-118"))
    if draws:
        vf += f";[v0]{','.join(draws)}[v]"
        label = "[v]"

    cmd = (["ffmpeg", "-y"] + seek + ["-i", raw, "-t", f"{seg:.2f}",
            "-filter_complex", vf, "-map", label, "-map", "0:a?",
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=44100"] + _ENC + [out])
    return _run_ffmpeg(cmd, "segment")


def _build_card(out, big, small, vo_path, card_seconds):
    """A full-screen title card. Audio = the narration (vo_path) if present, else
    silence; card length matches the narration (+pad) or card_seconds when silent."""
    C._ensure_tool("ffmpeg")
    font = C._ensure_font(bold=True)
    dur = float(card_seconds)
    if vo_path and os.path.exists(vo_path):
        d = C._probe(vo_path).get("duration_s")
        if d:
            dur = float(d) + 0.7
        audio_in = ["-i", vo_path]
    else:
        audio_in = ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]

    draws = []
    if font:
        if big:
            draws.append(C._drawtext(font, C._clean_drawtext(big), fontcolor="white",
                                     fontsize=96, borderw=7, bordercolor="black",
                                     x="(w-text_w)/2", y=int(LH * 0.34)))
        if small:
            draws.append(C._drawtext(font, C._clean_drawtext(small)[:48], fontcolor="0x9AD1FF",
                                     fontsize=52, borderw=4, bordercolor="black@0.8",
                                     x="(w-text_w)/2", y=int(LH * 0.56)))
    vf = f"[0:v]{','.join(draws)}[v]" if draws else "[0:v]null[v]"
    cmd = (["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=0x0B1220:s={LW}x{LH}:r=30"]
           + audio_in + ["-filter_complex", vf, "-map", "[v]", "-map", "1:a",
                         "-t", f"{dur:.2f}"] + _ENC + [out])
    return _run_ffmpeg(cmd, "card")


def _narration(rank, streamer, desc):
    who = streamer or "this creator"
    if desc:
        return f"At number {rank}, {who}. {desc}"
    return f"At number {rank}, {who} — you have to see this one."


def run(strategy, history, dry=False):
    cfg = strategy.get("longform", {}) or {}
    if not cfg.get("enabled", False):
        print("Long-form: disabled (strategy.longform.enabled=false) — nothing to do.")
        return

    wk = C.local_now(strategy).strftime("%G-W%V")
    lf_key = f"longform-{wk}"
    if any(p.get("slot_key") == lf_key for p in history.get("posts", [])):
        print("Long-form: already posted this ISO week — skipping.")
        return

    n = int(cfg.get("clip_count", 10))
    need = max(3, n // 2)
    picks = C._recent_top_posts(history, days=int(cfg.get("days", 7)), n=n)
    if len(picks) < need:
        print(f"Long-form: only {len(picks)} eligible clips this week — skipping "
              f"(need >= {need}).")
        return

    voiceover = bool(cfg.get("voiceover", False)) and tts.configured()
    grounding = bool(cfg.get("gemini_grounding", False)) and gemini.configured()
    seg_max = int(cfg.get("seg_max_s", 30))
    card_s = float(cfg.get("card_seconds", 3.5))
    gem_fps = cfg.get("gemini_fps", 10)
    games = [p.get("game") for p in picks if p.get("game")]
    game_label = strategy["games"].get(max(set(games), key=games.count), "gaming") if games else "gaming"
    n_used = len(picks)
    title = str(cfg.get("title", "Top {n} {game} Clips of the Week")).format(
        n=n_used, game=game_label)
    print(f"Long-form: building '{title}' from {n_used} clips "
          f"(voiceover={voiceover}, gemini={grounding})")

    tmp = tempfile.gettempdir()
    pieces = []

    # Intro card
    intro_vo = None
    if voiceover:
        intro_vo = tts.synthesize(
            f"The top {n_used} {game_label} clips of the week. Let's count them down.",
            os.path.join(tmp, "lf_vo_intro.wav"))
    pieces.append(_build_card(os.path.join(tmp, "lf_card_intro.mp4"),
                              title, "Let's count them down", intro_vo, card_s))

    # ranked = best-first; play worst -> best so #1 is revealed last
    for idx in range(len(picks) - 1, -1, -1):
        post = picks[idx]
        rank = idx + 1
        streamer = post.get("broadcaster") or post.get("author") or ""
        raw = os.path.join(tmp, f"lf_raw_{rank}_{C._safe_key(post['clip_id'])}.mp4")
        try:
            C.download_clip({"id": post["clip_id"], "url": post.get("clip_url"),
                             "source": post.get("source", "twitch")}, raw)
        except Exception as e:
            tail = (str(e).splitlines() or [""])[-1][:120]
            print(f"  #{rank}: download failed ({post.get('clip_id')}): {tail}; skipping")
            continue
        seg = _build_segment(raw, os.path.join(tmp, f"lf_seg_{rank}.mp4"),
                             rank, streamer, seg_max)

        desc = None
        if grounding:
            desc = gemini.analyze_clip(
                seg, hint=f"{game_label}; titled '{post.get('title') or ''}'", fps=gem_fps)
        card_vo = None
        if voiceover:
            card_vo = tts.synthesize(_narration(rank, streamer, desc),
                                     os.path.join(tmp, f"lf_vo_{rank}.wav"))
        pieces.append(_build_card(os.path.join(tmp, f"lf_card_{rank}.mp4"),
                                  f"#{rank} - {streamer}", desc, card_vo, card_s))
        pieces.append(seg)

    if len([p for p in pieces if "_seg_" in p]) < need:
        print("Long-form: too few clips survived download/build — skipping.")
        return

    # Outro card
    outro_vo = None
    if voiceover:
        outro_vo = tts.synthesize(
            f"That's the top {n_used}. Follow at clipkroniek for daily clips, and I'll "
            "see you next week.", os.path.join(tmp, "lf_vo_outro.wav"))
    pieces.append(_build_card(os.path.join(tmp, "lf_card_outro.mp4"),
                              "Thanks for watching", "Follow @clipkroniek - new clips daily",
                              outro_vo, max(card_s, 4.0)))

    comp = os.path.join(tmp, "ck_longform.mp4")
    C._concat_reels(pieces, comp)
    total_s = C._probe(comp).get("duration_s")
    print(f"Built long-form: {comp} ({os.path.getsize(comp) // 1024} KB, {total_s}s)")

    if dry:
        print("\nDRY_RUN=1 — built the long-form; not uploading.")
        if C.r2_configured():
            print("Preview: " + C.host_file_r2(comp, "previews/ck_longform.mp4", "video/mp4"))
        return

    if not youtube.configured():
        print("Long-form built but YT_* secrets are absent — nothing to upload to. "
              "(Long-form is YouTube-only.)")
        return

    tags = []
    for g in dict.fromkeys(games):
        tags += C._game_hashtags(strategy, g)
    hashtag_line = " ".join(dict.fromkeys(tags))
    description = "\n\n".join([
        f"{title} — the week's best clips, ranked.",
        "Every clip credited to its original creator. Follow @clipkroniek for daily clips.",
        hashtag_line,
    ])
    vid = youtube.upload_short(comp, title=title[:100], description=description,
                              tags=[game_label.lower(), "gaming", "clips", "compilation",
                                    "best of"],
                              category_id="20",
                              privacy=(cfg.get("privacy", "private")))
    vid_id = vid.get("id")
    status = (vid.get("status") or {}).get("privacyStatus")
    url = f"https://youtu.be/{vid_id}" if vid_id else None
    print(f"Uploaded long-form to YouTube: {url} (privacy={status})")
    if status == "private" and cfg.get("privacy", "private") != "private":
        print("  NOTE: forced private — needs the YouTube API compliance audit for public.")

    local = C.local_now(strategy)
    history.setdefault("posts", []).append({
        "slot_key": lf_key,
        "date_utc": C.now_utc().isoformat(),
        "local_date": local.date().isoformat(),
        "weekday": local.weekday(),
        "slot_hour": local.hour,
        "game": "+".join(dict.fromkeys(games)),
        "region": "mixed",
        "source": "compilation",
        "format": "longform",
        "clip_id": lf_key,
        "clip_ids": [p.get("clip_id") for p in picks],
        "duration_s": round(float(total_s), 2) if total_s else None,
        "voiceover": voiceover,
        "grounded": grounding,
        "youtube": {"id": vid_id, "url": url, "privacy": status},
        "media_id": None,
        "metrics": {},
        "measured_at": None,
    })
    C.save_history(history)
    print("Recorded long-form to history.json")
