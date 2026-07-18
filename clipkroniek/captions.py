#!/usr/bin/env python3
"""
Free animated word-synced captions — builds an ASS subtitle file from the Whisper
word timestamps we already have, for ffmpeg's built-in libass to burn in.

TikTok/Reels style: 1-3 words on screen, the currently-spoken word pops (112%->100%)
and highlights yellow in sync. $0, no new deps (libass ships with ffmpeg). Language-
aware font so the Japanese/Korean VTuber clips render too (Noto Sans CJK) instead of
tofu boxes.
"""
import os
import html
import subprocess

# Brand caption font, bundled in-repo (internal family name = "Anton").
ANTON = "Anton"
CJK_FONT = {"ja": "Noto Sans CJK JP", "ko": "Noto Sans CJK KR",
            "zh": "Noto Sans CJK SC", "zh-cn": "Noto Sans CJK SC",
            "zh-tw": "Noto Sans CJK TC"}
_CJK = ("ja", "japanese", "ko", "korean", "zh", "chinese", "zh-cn", "zh-tw", "yue")

HIGHLIGHT = "&H0004C2F7&"          # yellow #F7C204 in ASS BGR


def _is_cjk(lang):
    l = (lang or "").strip().lower()
    return l in _CJK or any(l.startswith(c) for c in ("ja", "ko", "zh"))


def _cjk_font(lang):
    l = (lang or "").strip().lower()
    for k, v in CJK_FONT.items():
        if l.startswith(k):
            return v
    return "Noto Sans CJK JP"


def _ensure_cjk_font():
    """Install Noto Sans CJK on the runner if absent (only for JP/KR/ZH captions)."""
    try:
        out = subprocess.run(["fc-list"], capture_output=True, text=True,
                             timeout=30).stdout
        if "Noto Sans CJK" in out or "NotoSansCJK" in out:
            return True
    except Exception:
        pass
    try:
        print("  installing fonts-noto-cjk for CJK captions...")
        subprocess.run(["sudo", "apt-get", "install", "-y", "-qq", "fonts-noto-cjk"],
                       check=True, timeout=300)
        subprocess.run(["fc-cache", "-f"], timeout=120)
        return True
    except Exception as e:
        print(f"  (noto-cjk install failed: {e})")
        return False


def _ts(sec):
    sec = max(0.0, float(sec))
    return f"{int(sec // 3600)}:{int(sec % 3600 // 60):02d}:{sec % 60:05.2f}"


def _esc(text):
    t = html.unescape(text or "").replace("\n", " ").replace("\r", " ")
    t = t.replace("{", "(").replace("}", ")")     # braces delimit ASS override blocks
    return " ".join(t.split()).strip()


def build_ass(words, out_path, reel_dur, *, language=None, offset=0.0,
              max_words=3, max_chars=18, font_size=80, pos_y=1180, upper=True):
    """words = [{word,start,end}] in SOURCE time. `offset` (the trim start) is
    subtracted so caption times are reel-relative. `upper` renders ALL CAPS (the Anton
    TikTok look; a no-op for CJK). Returns out_path, or None if no usable words."""
    ws = []
    for w in words or []:
        st = float(w.get("start") or 0.0) - offset
        en = float(w.get("end") or st) - offset
        tok = _esc(w.get("word"))
        if upper:
            tok = tok.upper()
        if not tok or en <= 0 or st >= reel_dur:
            continue
        ws.append({"t": tok, "s": max(0.0, st), "e": min(reel_dur, max(en, st + 0.05))})
    if not ws:
        return None

    # group into lines of <= max_words / max_chars
    lines, cur, curlen = [], [], 0
    for w in ws:
        wl = len(w["t"])
        if cur and (len(cur) >= max_words or curlen + wl + 1 > max_chars):
            lines.append(cur)
            cur, curlen = [], 0
        cur.append(w)
        curlen += wl + 1
    if cur:
        lines.append(cur)

    cjk = _is_cjk(language)
    if cjk:
        _ensure_cjk_font()
    font = _cjk_font(language) if cjk else ANTON

    events = []
    for li, line in enumerate(lines):
        line_end = lines[li + 1][0]["s"] if li + 1 < len(lines) else line[-1]["e"]
        line_end = min(reel_dur, max(line_end, line[-1]["e"]))
        toks = [w["t"] for w in line]
        pre = f"{{\\an5\\pos(540,{pos_y})}}"
        for wi, w in enumerate(line):
            start = w["s"]
            end = line[wi + 1]["s"] if wi + 1 < len(line) else line_end
            if end <= start:
                end = start + 0.15
            parts = []
            for k, tok in enumerate(toks):
                if k == wi:
                    parts.append(f"{{\\c{HIGHLIGHT}\\fscx112\\fscy112"
                                 f"\\t(0,90,\\fscx100\\fscy100)}}{tok}{{\\r}}")
                else:
                    parts.append(tok)
            events.append((start, end, pre + " ".join(parts)))

    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\nYCbCr Matrix: TV.709\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding\n"
        f"Style: Cap,{font},{font_size},&H00FFFFFF,&H0000FFFF,&H00000000,&H64000000,"
        "-1,0,0,0,100,100,0,0,1,4,1,5,60,60,0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, "
        "Text\n"
    )
    body = "".join(f"Dialogue: 0,{_ts(s)},{_ts(e)},Cap,,0,0,0,,{txt}\n"
                   for s, e, txt in events)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + body)
    return out_path
