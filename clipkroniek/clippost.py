#!/usr/bin/env python3
"""
Clipkroniek — autonomous gaming-clip repost poster (GTA V + VALORANT).

The POSTING half of a self-optimizing loop (sibling of Brine & Bloom):

    strategy.json  ->  clippost.py (this file)  ->  history.json
         ^                                              |
         |                                              v
    analyze.py  <-----------  Instagram Insights  <-----+

Each run asks: "is now a scheduled slot I haven't already posted?" If yes, it:
  1. discovers fresh (<24h) Twitch clips for the slot's game, filtered to the
     slot's region (by clip language) and ranked by views, deduped vs history;
  2. downloads the top clip (yt-dlp) and reformats it to a 9:16 Reel with a
     burned-in text hook (blurred-fill background, ffmpeg);
  3. has Claude write the caption; credits the original creator;
  4. hosts the mp4 on R2 (Reels need a public URL) and publishes it;
  5. records the post and (on Actions) commits history.json back.

Env (GitHub Actions secrets — see README):
    ANTHROPIC_API_KEY, IG_ACCESS_TOKEN, TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET,
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET,
    R2_PUBLIC_BASE_URL   (IG_USER_ID optional — derived from the token.)

Control env (optional):
    DRY_RUN=1        discover + build the reel + print, do NOT host/publish
    DISCOVER_ONLY=1  discover + write caption + print, do NOT download/build
                     (local smoke test — only needs ANTHROPIC + TWITCH creds)
    FORCE=1          ignore the schedule, run today's (or the first) slot now
    SLOT_GAME=...    override the slot's game (gta|valorant)
    SLOT_REGION=...  override the slot's region (western|asian)
"""
import os
import sys
import json
import glob
import math
import time
import shutil
import hashlib
import tempfile
import datetime
import subprocess
import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:                      # pragma: no cover (Python < 3.9)
    ZoneInfo = None

import twitch
import reddit
import youtube
import vision
import transcribe
import captions
import telegram

# =============================================================================
# CONFIG — brand/knobs live here; timing/cadence/mix live in strategy.json.
# =============================================================================
BRAND_NAME = "Clipkroniek"
STRATEGY_FILE = "strategy.json"
HISTORY_FILE = "history.json"

GRAPH_HOST = "https://graph.instagram.com"
GRAPH_VERSION = "v23.0"
CLAUDE_MODEL = "claude-sonnet-4-6"

MAX_HASHTAGS = 5
MAX_CAPTION_CHARS = 2200
REEL_W, REEL_H = 1080, 1920

# Optional transparency line appended to captions. Attribution to the original
# creator is always added separately (credit line), so default this OFF.
DISCLOSURE = os.environ.get("CK_DISCLOSURE", "")

R2_ENV = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
          "R2_BUCKET", "R2_PUBLIC_BASE_URL"]

# Fallback hashtag pools per game (Claude usually overrides these).
GAME_HASHTAGS = {
    "gta": ["#gtarp", "#gta6", "#gtaonline", "#gtaclips", "#nopixel", "#gta5"],
    "valorant": ["#valorant", "#valorantclips", "#valorantmoments",
                 "#valoranthighlights", "#radiant"],
}

_RETRY_STATUS = {429, 500, 502, 503, 504}


def _load_dotenv(path=None):
    """Populate os.environ from a local gitignored .env (dev convenience). Never
    overwrites a var already set, so GitHub Actions secrets always win. No-op if
    the file is absent (as on Actions)."""
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and v:
                    os.environ.setdefault(k, v)
    except Exception:
        pass


_load_dotenv()


# =============================================================================
# Generic helpers
# =============================================================================
def env(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"Missing required environment variable: {name}")
    return v


def http(method, url, *, retries=4, backoff=3, **kwargs):
    """requests with retry/backoff on 429/5xx/network blips."""
    last = None
    for attempt in range(retries + 1):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code in _RETRY_STATUS and attempt < retries:
                wait = backoff * (2 ** attempt)
                print(f"  {resp.status_code} from {url.split('?')[0]} — "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
                continue
            return resp
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))
                continue
            raise
    if last:
        raise last
    return resp


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_ts(s):
    """Parse an ISO/RFC3339 timestamp (Twitch 'created_at', our 'date_utc') to an
    aware datetime, or None."""
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def load_strategy():
    s = _load_json(STRATEGY_FILE, None)
    if not s or "slots" not in s:
        sys.exit(f"{STRATEGY_FILE} is missing or has no slots.")
    return s


def load_history():
    h = _load_json(HISTORY_FILE, {"posts": []})
    if "posts" not in h:
        h["posts"] = []
    return h


def save_history(h):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(h, f, indent=2, ensure_ascii=False)


# =============================================================================
# Timezone + slot gating
# =============================================================================
def _tz(strategy):
    name = strategy.get("timezone", "UTC")
    if ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.timezone.utc


def local_now(strategy):
    return now_utc().astimezone(_tz(strategy))


def _slot_key(slot, local_dt):
    return f"{local_dt.date().isoformat()}-w{int(slot['weekday'])}-{int(slot['hour']):02d}"


def _stable_jitter(key, jitter):
    """Deterministic per-slot-per-day minute offset in [-jitter, +jitter] so the
    scheduled time isn't robotically fixed but also doesn't drift between runs."""
    if jitter <= 0:
        return 0
    d = int(hashlib.md5(key.encode()).hexdigest(), 16)
    return (d % (2 * jitter + 1)) - jitter


def _posts_today(history, local_dt):
    d = local_dt.date().isoformat()
    return sum(1 for p in history.get("posts", []) if p.get("local_date") == d)


def find_due_slot(strategy, history):
    """(slot, slot_key) if now is within a slot's forward tolerance window, not
    already posted, and under the daily cap; else (None, None)."""
    local = local_now(strategy)
    tol = int(strategy.get("tolerance_minutes", 150))
    jitter = int(strategy.get("jitter_minutes", 10))
    cap = int(strategy.get("cadence", {}).get("max_posts_per_day", 1))
    posted = {p.get("slot_key") for p in history.get("posts", [])}
    if _posts_today(history, local) >= cap:
        return None, None
    for slot in strategy.get("slots", []):
        if int(slot["weekday"]) != local.weekday():
            continue
        key = _slot_key(slot, local)
        if key in posted:
            continue
        sched = local.replace(hour=int(slot["hour"]),
                              minute=int(slot.get("minute", 0)),
                              second=0, microsecond=0)
        sched += datetime.timedelta(minutes=_stable_jitter(key, jitter))
        delta_min = (local - sched).total_seconds() / 60.0
        if 0 <= delta_min <= tol:
            return slot, key
    return None, None


def forced_slot(strategy):
    local = local_now(strategy)
    todays = [s for s in strategy["slots"] if int(s["weekday"]) == local.weekday()]
    slot = (todays or strategy["slots"])[0]
    return slot, _slot_key(slot, local) + "-forced"


# =============================================================================
# Discovery (Twitch)
# =============================================================================
def _region_langs(strategy, region):
    return set(strategy.get("regions", {}).get(region, ["en"]))


def _reddit_ua():
    return (os.environ.get("REDDIT_USER_AGENT")
            or "web:clipkroniek:1.0 (by /u/clipkroniek)")


def _curated_logins(strategy, slot):
    """Game-agnostic curated streamer roster. Their clips are game-filtered in
    _discover_twitch, so ANY listed streamer who happened to play this slot's game
    gets picked up automatically — no per-game lists. Western slots only (the list
    is English-speaking; the Asian probes stay on top-by-views)."""
    if slot.get("region") != "western":
        return []
    cur = strategy.get("curated_streamers", [])
    if isinstance(cur, dict):                     # back-compat: flatten an old per-game map
        cur = [s for lst in cur.values() for s in lst]
    return list(cur)


def _discover_twitch(strategy, slot, posted_ids, hours=None, langs=None, pages=None):
    """Twitch candidates = the game-wide top clips, tagged with is_curated (roster
    boost, #9), age_hours + velocity (views/hour, #6). Parameterized so the
    escalation ladder (#11) can re-query with a wider window/langs. Extras:
      - #9 prune: clips from org/tournament channels (RiotGames, ESL, LCK, ...) are
        dropped — they aren't creators we can credit-grow toward.
      - #10 Asian: for Asian slots, top up the game-wide list with recent clips
        pulled directly from an Asian-creator roster (the global top list skews to
        the biggest names), and count language-less clips instead of assuming 'en'.
    """
    cid = env("TWITCH_CLIENT_ID")
    secret = env("TWITCH_CLIENT_SECRET")
    token = twitch.get_app_token(cid, secret, http)
    game_name = strategy["games"][slot["game"]]
    game_id = twitch.resolve_game_id(game_name, cid, token, http)

    hours = int(hours or strategy.get("recency_hours", 24))
    ended = now_utc()
    started = ended - datetime.timedelta(hours=hours)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    s_iso, e_iso = started.strftime(fmt), ended.strftime(fmt)
    if pages is None:
        pages = int(strategy.get("discover_pages_asian", 20)
                    if slot.get("region") == "asian"
                    else strategy.get("discover_pages", 8))

    clips = twitch.get_recent_clips(game_id, s_iso, e_iso, cid, token, http, pages=pages)

    # #10 Asian top-up: pull each Asian-roster streamer's recent clips (any game),
    # keep the ones for THIS game/window not already in the pool.
    n_topup = 0
    if slot.get("region") == "asian":
        roster = (strategy.get("asian_streamers") or [])[:15]
        if roster:
            try:
                ids = twitch.resolve_user_ids(roster, cid, token, http)
                seen = {c.get("id") for c in clips}
                for bid in ids.values():
                    try:
                        extra = twitch.get_broadcaster_clips(
                            bid, s_iso, e_iso, cid, token, http, first=20)
                    except Exception:
                        continue
                    for c in extra:
                        if str(c.get("game_id")) == str(game_id) and c.get("id") not in seen:
                            seen.add(c.get("id"))
                            clips.append(c)
                            n_topup += 1
            except Exception as e:
                print(f"    (asian roster top-up failed: {e})")

    # Resolve the curated roster (Western only) and the org-channel prune list to
    # broadcaster ids once, then tag/drop pool clips by broadcaster id.
    def _ids(logins):
        if not logins:
            return set()
        try:
            return {str(v) for v in
                    twitch.resolve_user_ids(logins, cid, token, http).values()}
        except Exception as e:
            print(f"    (roster resolve failed: {e})")
            return set()

    curated_ids = _ids(_curated_logins(strategy, slot))
    prune_ids = _ids(strategy.get("prune_broadcasters") or [])

    langs = set(langs) if langs else _region_langs(strategy, slot["region"])
    min_d = float(strategy.get("min_duration_s", 5))
    max_d = float(strategy.get("max_duration_s", 60))
    now = now_utc()
    base, n_no_lang, n_pruned = [], 0, 0
    for cl in clips:
        if cl.get("id") in posted_ids:
            continue
        if str(cl.get("broadcaster_id")) in prune_ids:
            n_pruned += 1
            continue
        lang = cl.get("language")
        if not lang:
            n_no_lang += 1                       # #10: don't silently treat as 'en'
        if (lang or "en") not in langs:
            continue
        dur = cl.get("duration")
        if dur is not None and (float(dur) < min_d or float(dur) > max_d):
            continue
        created = _parse_ts(cl.get("created_at"))
        age_h = (max((now - created).total_seconds() / 3600.0, 0.5)
                 if created else float(hours))
        cl["age_hours"] = round(age_h, 2)
        cl["velocity"] = round(float(cl.get("view_count") or 0) / age_h, 2)
        cl["source"] = "twitch"
        cl["is_curated"] = str(cl.get("broadcaster_id")) in curated_ids
        base.append(cl)
    n_cur = sum(1 for c in base if c.get("is_curated"))
    label = (f"{game_name} {slot['region']}={sorted(langs)} last {hours}h "
             f"({len(clips)} clips/{pages}pg" + (f"+{n_topup}topup" if n_topup else "")
             + f"; -{n_pruned}org; roster {len(_curated_logins(strategy, slot))} -> "
             f"{len(base)} cand, {n_cur} curated, {n_no_lang} no-lang)")
    return base, label


def _discover_reddit(strategy, slot, posted_ids):
    """Candidate list from Reddit: top-of-day v.redd.it clips across the game's
    subreddits (community-upvoted). Reddit is English/Western by nature."""
    subs = strategy.get("reddit_subreddits", {}).get(slot["game"], [])
    if not subs:
        return [], f"no subreddits configured for {slot['game']}"
    hours = int(strategy.get("recency_hours", 24))
    min_d = float(strategy.get("min_duration_s", 5))
    max_d = float(strategy.get("max_duration_s", 60))
    cutoff = (now_utc() - datetime.timedelta(hours=hours)).timestamp()
    ua = _reddit_ua()
    token = reddit.get_app_token(env("REDDIT_CLIENT_ID"),
                                 env("REDDIT_CLIENT_SECRET"), ua, http)
    posts = reddit.get_top_video_posts(subs, token, ua, http,
                                       min_d=min_d, max_d=max_d, cutoff_ts=cutoff)
    base = [p for p in posts if p.get("id") not in posted_ids]
    label = f"{slot['game']} r/{'+r/'.join(subs)} top<{hours}h ({len(posts)} videos)"
    return base, label


def _duration_weight(dur):
    """#11: bands favouring loop-friendly lengths. 8-30s is the sweet spot for
    Reels; very short = too thin, long = weak completion."""
    if dur is None:
        return 0.8
    d = float(dur)
    if d < 8:
        return 0.6
    if d <= 30:
        return 1.0
    if d <= 45:
        return 0.7
    if d <= 60:
        return 0.4
    return 0.2


def _broadcaster_recency_weight(history, name):
    """#9 variety: down-weight a broadcaster we posted very recently so the feed
    isn't the same 3 faces. x0.3 if <3d ago, x0.6 if <7d, else 1.0."""
    if not name:
        return 1.0
    now, newest = now_utc(), None
    for p in history.get("posts", []):
        if (p.get("broadcaster") or "").lower() == name.lower():
            t = _parse_ts(p.get("date_utc"))
            if t and (newest is None or t > newest):
                newest = t
    if not newest:
        return 1.0
    age_d = (now - newest).total_seconds() / 86400.0
    if age_d < 3:
        return 0.3
    if age_d < 7:
        return 0.6
    return 1.0


def discover_clip(strategy, slot, history):
    """Ranked POOL (not just the top clip) of not-yet-posted candidates for this
    slot, so the Claude judge (#7) can pick and main can fall back on a download
    failure (#22a). Score = velocity (views/hour, #6) in log-domain, weighted by
    clip duration (#11), broadcaster recency (#9) and a curated-roster boost (#9).
    Twitch uses an escalation ladder (#11) so a quiet game/region still yields a
    post rather than going dark."""
    posted_ids = {p.get("clip_id") for p in history.get("posts", [])}
    min_v = int(strategy.get("min_view_count", 50))
    hard_floor = int(strategy.get("hard_view_floor", 10))
    source = slot.get("source", "twitch")
    boost = float(strategy.get("curated_boost", 2.0))

    def score(cl):
        vel = float(cl.get("velocity") or cl.get("view_count") or 0)
        s = math.log10(vel + 1.0)
        s *= _duration_weight(cl.get("duration"))
        s *= _broadcaster_recency_weight(history, cl.get("broadcaster_name"))
        if cl.get("is_curated"):
            s *= boost
        return s

    def rank(base):
        for c in base:
            c["_score"] = round(score(c), 4)
        base.sort(key=lambda c: c["_score"], reverse=True)
        return base

    def qualifying(base):
        return [c for c in base if int(c.get("view_count") or 0) >= min_v]

    def finalize(pool, cap=12):
        pool = pool[:cap]
        for c in pool:
            c.setdefault("source", source)
        if pool:
            top = pool[0]
            tag = "CURATED " if top.get("is_curated") else ""
            print(f"  top: {tag}{top.get('view_count')} views @ "
                  f"{top.get('velocity')}/h | {top.get('broadcaster_name')} | "
                  f"{(top.get('title') or '')[:50]}")
        return pool

    if source == "reddit":
        base, label = _discover_reddit(strategy, slot, posted_ids)
        for c in base:                                # no created-at velocity on reddit
            c.setdefault("velocity", float(c.get("view_count") or 0))
        base = rank(base)
        pool = qualifying(base) or [c for c in base
                                    if int(c.get("view_count") or 0) >= hard_floor]
        print(f"  discovery[reddit]: {label}; {len(base)} cand, {len(pool)} in pool")
        return finalize(pool)

    # --- Twitch escalation ladder ---------------------------------------
    tried = []
    base, label = _discover_twitch(strategy, slot, posted_ids)
    base = rank(base)
    tried.append(label)
    pool = qualifying(base)

    if not pool:                                      # step 2: widen window to 48h
        b, l = _discover_twitch(strategy, slot, posted_ids, hours=48)
        base = rank(b)
        tried.append(l + " [48h]")
        pool = qualifying(base)

    if not pool and slot.get("region") == "western":  # step 3: widen European langs
        wide = strategy.get("wide_langs", ["en", "de", "fr", "es", "pt"])
        b, l = _discover_twitch(strategy, slot, posted_ids, hours=48, langs=wide)
        base = rank(b)
        tried.append(l + " [wide-langs]")
        pool = qualifying(base)

    if not pool:                                      # step 4: best-available or none
        pool = [c for c in base if int(c.get("view_count") or 0) >= hard_floor]

    print(f"  discovery[twitch] ladder: {' | '.join(tried)}")
    print(f"  -> {len(pool)} in pool (min_v={min_v}, curated x{boost:g})")
    return finalize(pool)


# =============================================================================
# Download + reformat to a 9:16 Reel with a burned-in hook
# =============================================================================
def _ensure_tool(name):
    if name == "yt-dlp":
        try:
            import yt_dlp  # noqa: F401
            return
        except ImportError:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "yt-dlp"],
                           check=True)
            return
    if shutil.which(name):
        return
    print(f"Installing {name}...")
    subprocess.run(["sudo", "apt-get", "update", "-qq"], check=True)
    pkgs = ["ffmpeg", "fonts-dejavu-core"] if name == "ffmpeg" else [name]
    subprocess.run(["sudo", "apt-get", "install", "-y", "-qq"] + pkgs, check=True)


def _font(bold=True):
    paths = ([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/Arial.ttf",
    ] if bold else [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/Arial.ttf",
    ])
    for p in paths:
        if os.path.exists(p):
            return p
    return None


def _ensure_font(bold=True):
    """Return a usable TTF, installing fonts-dejavu-core on demand. ubuntu-latest
    ships ffmpeg preinstalled, so `_ensure_tool('ffmpeg')` never reaches its font
    install — without this, _font() returns None and every drawtext overlay would
    silently no-op. Returns the path, or None only if install genuinely failed."""
    f = _font(bold)
    if f:
        return f
    if sys.platform.startswith("linux"):
        try:
            print("Installing fonts-dejavu-core...")
            subprocess.run(["sudo", "apt-get", "update", "-qq"], check=False)
            subprocess.run(["sudo", "apt-get", "install", "-y", "-qq",
                            "fonts-dejavu-core"], check=True)
        except Exception as e:
            print(f"  (font install failed: {e})")
        f = _font(bold)
    return f


def _probe(path):
    """ffprobe -> {duration_s, fps, width, height}. Best-effort; keys None on
    failure. ffprobe ships with ffmpeg."""
    out = {"duration_s": None, "fps": None, "width": None, "height": None}
    _ensure_tool("ffmpeg")            # ffprobe ships with ffmpeg; may not be preinstalled
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,avg_frame_rate:format=duration",
             "-of", "json", path],
            capture_output=True, text=True, timeout=60)
        info = json.loads(r.stdout or "{}")
        st = (info.get("streams") or [{}])[0]
        fmt = info.get("format") or {}
        if fmt.get("duration"):
            out["duration_s"] = float(fmt["duration"])
        out["width"], out["height"] = st.get("width"), st.get("height")
        num, _, den = (st.get("avg_frame_rate") or "0/0").partition("/")
        try:
            out["fps"] = round(float(num) / float(den), 3) if float(den) else None
        except Exception:
            pass
    except Exception as e:
        print(f"  (ffprobe failed: {e})")
    return out


def _extract_frame(path, at_s, out):
    """Grab one JPEG frame at at_s (used for facecam detection). True on success."""
    _ensure_tool("ffmpeg")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{max(0.0, float(at_s)):.2f}", "-i", path,
         "-frames:v", "1", "-q:v", "3", out], timeout=60, stderr=subprocess.PIPE)
    return proc.returncode == 0 and os.path.exists(out)


def download_clip(clip, out_path):
    _ensure_tool("yt-dlp")
    # Twitch's private GQL intermittently returns a bad shape -> yt-dlp raises
    # KeyError('data'). It's usually transient, so retry with backoff and also try
    # the canonical clips.twitch.tv/<slug> URL form (Reddit clips use one url only).
    urls = [clip["url"]]
    slug = clip.get("id")
    if clip.get("source", "twitch") == "twitch" and slug:
        alt_url = f"https://clips.twitch.tv/{slug}"
        if alt_url not in urls:
            urls.append(alt_url)
    base = [sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--no-playlist",
            "--retries", "5", "--extractor-retries", "5", "--fragment-retries", "5",
            "-f", "mp4/bestvideo+bestaudio/best", "--merge-output-format", "mp4",
            "-o", out_path]
    last = ""
    for attempt in range(3):
        url = urls[attempt % len(urls)]
        proc = subprocess.run(base + [url], timeout=300, stderr=subprocess.PIPE)
        if proc.returncode == 0 and (os.path.exists(out_path)
                                     or glob.glob(out_path.rsplit(".", 1)[0] + ".*")):
            break
        last = proc.stderr.decode("utf-8", "replace")[-400:]
        print(f"  yt-dlp attempt {attempt + 1}/3 failed ({url}); retrying...")
        time.sleep(6 * (attempt + 1))
    if not os.path.exists(out_path):
        alt = glob.glob(out_path.rsplit(".", 1)[0] + ".*")
        if alt:
            os.replace(alt[0], out_path)
    if not os.path.exists(out_path):
        # Raise (not sys.exit) so the caller can fall back to the next candidate.
        raise RuntimeError(f"yt-dlp failed to download {clip.get('id')}:\n{last}")
    return out_path


def _audio_peak_window(path, dur, pre_s, post_s, min_s):
    """Twitch clips are captured AFTER the moment, so the payoff sits late — fatal
    for Reels' first-2s test. Find the loudest instant (gunfire/screams/hype) via
    ebur128 momentary loudness and return a (start, end, peak) window around it, so
    the reel cold-opens near the action and loops cleanly. None on failure."""
    if not dur or dur <= max(min_s, pre_s + post_s) + 1:
        return None
    _ensure_tool("ffmpeg")
    # Print ebur128 momentary loudness (lavfi.r128.M) per frame to a file via
    # ametadata — build-independent, unlike the console 't:/M:' lines which some
    # ffmpeg builds don't emit at the default log level.
    meta = path + ".r128.txt"
    try:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
             "-af", f"ebur128=metadata=1,ametadata=mode=print:file={meta}",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=180)
        with open(meta, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except Exception as e:
        print(f"  (peak analysis failed: {e})")
        return None
    finally:
        try:
            os.remove(meta)
        except OSError:
            pass
    import re
    best_t, best_m, cur_t = None, -1e9, None
    for line in lines:
        mt = re.search(r"pts_time:(-?[\d.]+)", line)
        if mt:
            cur_t = float(mt.group(1))
            continue
        mm = re.search(r"lavfi\.r128\.M=(-?[\d.]+)", line)
        if mm and cur_t is not None:
            loud = float(mm.group(1))
            if loud > best_m:
                best_m, best_t = loud, cur_t
    if best_t is None:
        return None
    start = max(0.0, best_t - pre_s)
    end = min(dur, best_t + post_s)
    if end - start < min_s:                    # widen symmetrically to the min length
        need = min_s - (end - start)
        start = max(0.0, start - need / 2)
        end = min(dur, end + need / 2)
    if end - start < min_s:
        return None
    return round(start, 2), round(end, 2), round(best_t, 2)


def _window_around(center, dur, pre_s, post_s, min_s):
    """Build a (start, end, center) trim window around `center`, widened to min_s."""
    if not dur:
        return None
    start = max(0.0, center - pre_s)
    end = min(dur, center + post_s)
    if end - start < min_s:
        need = min_s - (end - start)
        start = max(0.0, start - need / 2)
        end = min(dur, end + need / 2)
    if end - start < min_s:
        return None
    return round(start, 2), round(end, 2), round(center, 2)


def _transcript_cut_plan(transcript, dur, audio_peak_t, lead_min, lead_max):
    """Claude reads the timestamped transcript and returns {moment, lead_in_s}:
      - moment: the most CLIPPABLE verbal moment (reaction/exclamation/punchline) in
        seconds, or None if there's no clear verbal highlight.
      - lead_in_s: how many seconds of context to keep BEFORE the moment — MORE when
        the talk builds suspense / explains / leads up to what happens, LESS when it's
        mostly action with little relevant setup (bounded [lead_min, lead_max]).
    A HELPER only (audio peak still leads); handles any language. None on failure."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    segs = transcript.get("segments") or []
    if not segs:                                  # no segments -> group words into ~2s lines
        cur = None
        for w in (transcript.get("words") or []):
            wt = float(w.get("start") or 0)
            if cur is None or wt - cur["start"] >= 2.0:
                cur = {"start": wt, "text": (w.get("word") or "").strip()}
                segs.append(cur)
            else:
                cur["text"] = (cur["text"] + " " + (w.get("word") or "").strip()).strip()
    lines = "\n".join(f"[{s['start']:.1f}] {s['text']}"
                      for s in segs if s.get("text"))[:2500]
    if not lines.strip():
        return None
    system = (
        "You are given a timestamped transcript of a short gaming clip (any language). "
        "Return JSON ONLY: "
        '{"moment": <seconds or null>, "lead_in_s": <number>, "why": "<= 8 words"}.\n'
        "- moment: the timestamp of the single most CLIPPABLE verbal moment (a big "
        "reaction, exclamation, hype line or punchline); null if there's no clear "
        "verbal highlight (calm chatter/callouts/silence).\n"
        f"- lead_in_s: how many seconds of context to keep BEFORE the moment, between "
        f"{lead_min:g} and {lead_max:g}. Keep MORE (toward {lead_max:g}) when the talk "
        "builds suspense, explains, or clearly leads up to what happens. Keep LESS "
        f"(toward {lead_min:g}) when it's mostly action with little relevant setup.\n"
        f"The clip is {dur:.0f}s; the loudest audio moment is ~{audio_peak_t}s (a hint)."
    )
    try:
        resp = http("POST", "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": env("ANTHROPIC_API_KEY"),
                             "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json={"model": CLAUDE_MODEL, "max_tokens": 150, "system": system,
                          "messages": [{"role": "user", "content": lines}]},
                    timeout=60)
        resp.raise_for_status()
        text = "".join(b.get("text", "") for b in resp.json().get("content", [])
                       if b.get("type") == "text").strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.lstrip().startswith("json"):
                text = text.lstrip()[4:]
            text = text.strip()
        data = json.loads(text)
        out = {"moment": None, "lead_in_s": None}
        m = data.get("moment")
        if m is not None:
            m = float(m)
            out["moment"] = round(m, 2) if 0 <= m <= dur else None
        li = data.get("lead_in_s")
        if li is not None:
            out["lead_in_s"] = max(lead_min, min(lead_max, float(li)))
        return out
    except Exception as e:
        print(f"  (cut-plan hint failed: {e})")
        return None


def _decide_trim(strategy, slot_key, raw_path, dur):
    """Smart-trim as an A/B TEST (owner requirement: prove it lifts views first).
    mode 'ab' -> ~50/50 by a stable hash of slot_key; 'always' / 'off' also work.
    The audio peak LEADS the cut; if speech-to-text is available, the transcript's
    verbal-reaction moment REFINES it (a helper, never the leader). Returns
    (window_or_None, applied_bool, meta)."""
    cfg = strategy.get("smart_trim", {}) or {}
    mode = cfg.get("mode", "off")
    meta = {"transcript_used": False, "language": None, "verbal_moment": None}
    tr = None
    if mode == "off":
        return None, False, meta, tr
    on = (int(hashlib.md5(slot_key.encode()).hexdigest(), 16) % 2 == 0
          if mode == "ab" else True)
    if not on:
        return None, False, meta, tr

    post = float(cfg.get("post_s", 5))
    lead_min = float(cfg.get("lead_in_min", 3))
    lead_max = float(cfg.get("lead_in_max", 8))
    action_lead = float(cfg.get("lead_in_action", 3))
    default_lead = float(cfg.get("pre_s", 5))     # used when STT is unavailable
    min_s = float(strategy.get("min_duration_s", 5))

    # Pass the SMALLEST lead so short clips still yield a peak; the lead-in is chosen
    # dynamically below and the window is rebuilt.
    peak = _audio_peak_window(raw_path, dur, lead_min, post, min_s)
    center = peak[2] if peak else None
    meta["audio_peak"] = center
    lead = default_lead

    if transcribe.available(strategy):
        tr = transcribe.transcribe(raw_path, strategy)
        if tr and (tr.get("segments") or tr.get("words")):
            meta["language"] = tr.get("language")
            plan = _transcript_cut_plan(
                tr, float(dur or 0),
                center if center is not None else float(dur or 0) * 0.5,
                lead_min, lead_max)
            if plan:
                if plan.get("lead_in_s") is not None:
                    lead = plan["lead_in_s"]              # dynamic lead-in from the talk
                tv = plan.get("moment")
                meta["verbal_moment"] = tv
                if tv is not None:
                    meta["transcript_used"] = True
                    if center is None:
                        center = tv                       # no audio peak -> use speech
                    elif abs(tv - center) <= float(cfg.get("transcript_window_s", 5)):
                        center = round(0.6 * center + 0.4 * tv, 2)  # refine toward reaction
                    # else: they diverge -> keep the audio peak (it leads)
        else:
            lead = action_lead                            # no speech -> action clip -> short lead-in
            meta["no_speech"] = True
    meta["lead_in_s"] = round(lead, 2)

    if center is None:
        return None, False, meta, tr
    win = _window_around(center, dur, lead, post, min_s)  # dynamic lead-in applied here
    return win, (win is not None), meta, tr


def _clean_drawtext(s):
    """Sanitize an on-screen hook to a short, ffmpeg-drawtext-safe ASCII string."""
    s = "".join(ch for ch in (s or "") if ch.isalnum() or ch in " !?.-")
    return s.upper().strip()[:26] or "WATCH THIS"


def _credit_safe(s):
    """Sanitize a streamer credit for a small burned-in label — keeps @/_/./- and
    case (logins are lowercase), drops drawtext-unsafe chars. Empty -> ''."""
    s = "".join(ch for ch in (s or "") if ch.isalnum() or ch in "@_.- ")
    return s.strip()[:28]


def _streamer_credit(clip):
    """A small ASCII credit for the streamer: the Twitch login from the clip URL
    (ASCII, renders fine even when the display name is CJK), else broadcaster/author."""
    url = clip.get("url") or ""
    if "twitch.tv/" in url and "/clip/" in url:
        try:
            login = url.split("twitch.tv/")[1].split("/")[0].strip()
            if login:
                return "@" + login
        except Exception:
            pass
    if clip.get("author"):
        return "u/" + str(clip.get("author"))
    return _credit_safe(clip.get("broadcaster_name") or "")


def _drawtext(font, text, **opts):
    """Build a drawtext filter string. `text` must already be drawtext-safe (no
    ':' or ','); font path colons are escaped here."""
    fe = str(font).replace("\\", "/").replace(":", "\\:")
    parts = [f"fontfile='{fe}'", f"text='{text}'"]
    parts += [f"{k}={v}" for k, v in opts.items()]
    return "drawtext=" + ":".join(parts)


def _facecam_stack_graph(in_path, box):
    """Stacked 9:16 for reaction clips: an enlarged facecam panel on TOP of the full
    gameplay panel, over a blurred fill — so the streamer's face is fully visible
    instead of cropped out (and NOT overlaying the gameplay). `box`=(x,y,w,h) in 0-1
    fractions from vision.detect_facecam. Returns a graph ending in [base]."""
    pr = _probe(in_path)
    sw = int(pr.get("width") or 1920)
    sh = int(pr.get("height") or 1080)
    x, y, w, h = box
    # Trim slightly INWARD — vision boxes tend to run a touch loose and drag in HUD;
    # a small inset keeps the panel to just the webcam/avatar.
    inset = 0.006
    x = min(0.9, x + inset); y = min(0.9, y + inset)
    w = max(0.05, w - 2 * inset); h = max(0.05, h - 2 * inset)
    bx, by = int(x * sw), int(y * sh)
    bw, bh = max(16, int(w * sw)), max(16, int(h * sh))
    bx = min(bx, sw - bw); by = min(by, sh - bh)
    gp_h = int(round(REEL_W * sh / sw))          # gameplay panel height at 1080 wide (~608)
    # Fit the facecam INSIDE a 1080 x gp_h box, aspect preserved (never distorted,
    # never taller than the gameplay); it centres over the blur if narrower than 1080.
    fc_h = int(round(REEL_W * bh / bw)) if (bw / bh) >= (REEL_W / gp_h) else gp_h
    gap = 12
    total = fc_h + gp_h + gap
    top = max(20, (REEL_H - total) // 2)
    y_fc, y_gp = top, top + fc_h + gap
    bg = (f"[0:v]scale={REEL_W}:{REEL_H}:force_original_aspect_ratio=increase,"
          f"crop={REEL_W}:{REEL_H},boxblur=28:6,eq=brightness=-0.12[bg]")
    fc = (f"[0:v]crop={bw}:{bh}:{bx}:{by},"
          f"scale={REEL_W}:{gp_h}:force_original_aspect_ratio=decrease[fc]")
    # Gameplay panel: crop AWAY from the facecam's side so the cam isn't duplicated in
    # the bottom panel, then fill the panel (zoom-crop to fit). Split horizontally by
    # which side the cam sits on (facecams live on the left or right edge).
    fcx = box[0] + box[2] / 2.0
    if fcx < 0.5:
        gx0 = min(0.6, box[0] + box[2])          # keep gameplay to the RIGHT of the cam
        gx1 = 1.0
    else:
        gx0 = 0.0
        gx1 = max(0.4, box[0])                    # keep gameplay to the LEFT of the cam
    gcx = int(gx0 * sw)
    gcw = max(320, int((gx1 - gx0) * sw))
    gp = (f"[0:v]crop={gcw}:{sh}:{gcx}:0,"
          f"scale={REEL_W}:{gp_h}:force_original_aspect_ratio=increase,"
          f"crop={REEL_W}:{gp_h}[gp]")
    return (f"{bg};{fc};{gp};"
            f"[bg][fc]overlay=(W-w)/2:{y_fc}[t1];"
            f"[t1][gp]overlay=(W-w)/2:{y_gp}[base]")


def reformat_reel(in_path, out_path, strategy, slot, hook_text="", trim=None,
                  max_s=60, rank_label=None, cta=True, facecam=None,
                  credit=None, captions_ass=None):
    """9:16 Reel: blurred fill + a zoom-cropped gameplay overlay (#13, gameplay
    fills more of the frame), optional smart-trim to the action (#12), a permanent
    @clipkroniek watermark + a last-2.5s follow CTA (#1), loudness-normalised audio
    (#15), 60fps cap (#14) and a higher-quality encode. `trim`=(start,end[,peak])
    cuts to that window; None = whole clip. `rank_label` (e.g. '#1') is burned for
    the weekly Top-3 compilation. `facecam`=(x,y,w,h) normalized -> STACK the facecam
    above the gameplay (reaction clips) instead of the zoom-crop."""
    _ensure_tool("ffmpeg")
    game = slot.get("game")
    fg_zoom = float((strategy.get("fg_zoom", {}) or {}).get(
        game, (strategy.get("fg_zoom", {}) or {}).get("default", 1.3)))
    watermark = bool(strategy.get("brand_watermark", True))
    font = _ensure_font(bold=True) if (watermark or rank_label) else _font(bold=True)
    if watermark and not font:
        sys.exit("brand_watermark is on but no usable font could be installed — "
                 "aborting (an unbranded reel defeats the identity fix).")

    seek = ["-ss", f"{float(trim[0]):.2f}"] if trim else []
    if trim:
        dur = min(max(0.5, float(trim[1]) - float(trim[0])), float(max_s))
        length = ["-t", f"{dur:.2f}"]
    else:
        pr = _probe(in_path).get("duration_s")
        dur = min(float(pr), float(max_s)) if pr else float(max_s)
        length = ["-t", str(max_s)]

    if facecam:
        graph = _facecam_stack_graph(in_path, facecam)
    else:
        zw = int(REEL_W * fg_zoom)
        bg = (f"[0:v]scale={REEL_W}:{REEL_H}:force_original_aspect_ratio=increase,"
              f"crop={REEL_W}:{REEL_H},boxblur=26:6,eq=brightness=-0.10[bg]")
        fg = (f"[0:v]scale={zw}:-2,"
              f"crop=min(iw\\,{REEL_W}):min(ih\\,{REEL_H}):(iw-ow)/2:(ih-oh)/2[fg]")
        graph = f"{bg};{fg};[bg][fg]overlay=(W-w)/2:(H-h)/2[base]"
    label = "[base]"

    draws = []
    if credit and font:                        # small streamer credit, top-left
        draws.append(_drawtext(font, _credit_safe(credit), fontcolor="white@0.8",
                               fontsize=26, borderw=2, bordercolor="black@0.6",
                               x=28, y=26))
    hook = _clean_drawtext(hook_text) if hook_text else ""
    if hook and font:                          # optional legacy top hook (burn_hook)
        draws.append(_drawtext(font, hook, fontcolor="white", fontsize=68,
                               borderw=6, bordercolor="black@0.9", box=1,
                               boxcolor="black@0.45", boxborderw=26,
                               x="(w-text_w)/2", y=190))
    if watermark and font:
        draws.append(_drawtext(font, "@clipkroniek", fontcolor="white@0.85",
                               fontsize=34, borderw=3, bordercolor="black@0.65",
                               x="(w-text_w)/2", y=232))
        if cta:
            cta_at = max(0.0, dur - 2.5)
            draws.append(_drawtext(font, "FOLLOW FOR DAILY CLIPS", fontcolor="white",
                                   fontsize=46, borderw=5, bordercolor="black@0.9",
                                   box=1, boxcolor="black@0.5", boxborderw=20,
                                   x="(w-text_w)/2", y=1330,
                                   enable=f"gte(t\\,{cta_at:.2f})"))
    if rank_label and font:
        draws.append(_drawtext(font, _clean_drawtext(rank_label), fontcolor="yellow",
                               fontsize=96, borderw=8, bordercolor="black",
                               x="(w-text_w)/2", y=300))
    vfilters = list(draws)
    if captions_ass:                           # burn animated captions LAST (on top)
        vfilters.append(f"ass=filename={captions_ass}:fontsdir=fonts")
    if vfilters:
        graph += f";[base]{','.join(vfilters)}[out]"
        label = "[out]"

    cmd = (["ffmpeg", "-y"] + seek + ["-i", in_path,
            "-filter_complex", graph, "-map", label, "-map", "0:a?",
            "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
            "-c:v", "libx264", "-preset", "slow", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-fpsmax", "60"] + length + [out_path])
    proc = subprocess.run(cmd, timeout=420, stderr=subprocess.PIPE)
    if proc.returncode != 0 or not os.path.exists(out_path):
        sys.exit("ffmpeg reformat failed:\n"
                 + proc.stderr.decode("utf-8", "replace")[-1800:])
    return out_path


def build_cover(raw_path, out_path, strategy, slot, history, at_s):
    """Branded, episode-numbered cover (#3): a frame from the clip in the 9:16 look
    with 'CLIPKRONIEK' + '#N - GAME' burned into the grid-safe centre (a scroll-
    stopping, consistent profile grid). Returns out_path, or None on failure (the
    caller then falls back to thumb_offset). Episode number is on the COVER only —
    never in the caption (owner decision)."""
    _ensure_tool("ffmpeg")
    font = _ensure_font(bold=True)
    if not font:
        return None
    n = len(history.get("posts", [])) + 1
    game_label = _clean_drawtext(strategy["games"].get(slot["game"], slot["game"]))
    colors = strategy.get("cover_colors", {}) or {}
    color = str(colors.get(slot["game"], colors.get("default", "#00E5FF"))).replace("#", "0x")
    zw = int(REEL_W * 1.25)
    graph = (
        f"[0:v]scale={REEL_W}:{REEL_H}:force_original_aspect_ratio=increase,"
        f"crop={REEL_W}:{REEL_H},boxblur=30:8,eq=brightness=-0.18[bg];"
        f"[0:v]scale={zw}:-2,crop=min(iw\\,{REEL_W}):min(ih\\,{REEL_H}):"
        f"(iw-ow)/2:(ih-oh)/2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[base];"
        f"[base]"
        + _drawtext(font, "CLIPKRONIEK", fontcolor="white", fontsize=64, borderw=6,
                    bordercolor="black", x="(w-text_w)/2", y=360) + ","
        + _drawtext(font, f"#{n} - {game_label}", fontcolor=color, fontsize=52,
                    borderw=5, bordercolor="black", x="(w-text_w)/2", y=1470)
        + "[out]"
    )
    cmd = ["ffmpeg", "-y", "-ss", f"{max(0.0, float(at_s)):.2f}", "-i", raw_path,
           "-filter_complex", graph, "-map", "[out]",
           "-frames:v", "1", "-q:v", "3", out_path]
    proc = subprocess.run(cmd, timeout=120, stderr=subprocess.PIPE)
    if proc.returncode != 0 or not os.path.exists(out_path):
        print("  (cover build failed: "
              + proc.stderr.decode("utf-8", "replace")[-300:] + ")")
        return None
    return out_path


# =============================================================================
# Caption (Claude)
# =============================================================================
def _game_hashtags(strategy, game):
    """Learned hashtags for a game (analyzer writes strategy.game_hashtags) with the
    hardcoded pool as fallback."""
    return ((strategy.get("game_hashtags", {}) or {}).get(game)
            or GAME_HASHTAGS.get(game, []))


def call_claude(pool, slot, strategy, human_desc=None, chosen_index=None):
    """One call does two jobs: (#7) JUDGE the candidate clips by metadata and pick
    the most viral-looking gameplay clip, and write the post copy — an SEO caption
    (#19a), a generic hook, niche hashtags, and a first-comment question (#4).
    Claude CANNOT watch the videos, so it stays general. Learnings + caption
    strategy are fed in as advisory (#16).

    Human-in-the-loop: when `chosen_index` is set, that clip is ALREADY picked by the
    owner; Claude just writes the copy for it. If `human_desc` is given (the owner
    watched it and described the moment), Claude MAY write a SPECIFIC caption about that
    exact moment — the one case where naming what happens is allowed and encouraged."""
    game_name = strategy["games"][slot["game"]]
    tags_pool = " ".join(_game_hashtags(strategy, slot["game"]))
    cands = [{
        "index": i,
        "title": c.get("title"),
        "streamer": c.get("broadcaster_name"),
        "views": c.get("view_count"),
        "views_per_hour": c.get("velocity"),
        "duration_s": c.get("duration"),
        "language": c.get("language"),
        "curated": bool(c.get("is_curated")),
    } for i, c in enumerate(pool[:8])]

    system = (
        "You are the editor for 'Clipkroniek', an Instagram page that reposts the "
        "best FRESH gaming clips WITH creator credit. You are given METADATA for "
        "several candidate clips (you CANNOT watch them) and must (1) PICK the one "
        "most likely to go viral as a 9:16 Reel, and (2) write the post copy.\n"
        "PICK guidance: prefer high views_per_hour (momentum) and a clean, punchy, "
        "SFW title that reads like real gameplay (insane play / clutch / funny fail "
        "/ chaos). AVOID titles that look like reaction/watch-along, gambling or "
        "slots, giveaways, drama, pure clickbait, or anything sketchy/NSFW. A "
        "shorter clip (8-30s) usually loops better.\n"
        "Return a JSON object ONLY (no prose, no markdown):\n"
        '{"pick_index": <int>, "why_pick": "one short line", "hook": "...", '
        '"caption": "...", "hashtags": ["#..."], "comment_question": "..."}\n'
        "CRITICAL: because you can't see the clip, NEVER invent specific events, "
        "outcomes, kill counts, or who-did-what. Stay intriguing but GENERAL.\n"
        f"- caption: 1-2 short punchy lines that NAME the game plainly and include a "
        f"natural search phrase people type (e.g. '{game_name} clips', 'best "
        f"{game_name} moments') — caption SEO matters more than hashtags now. "
        "English. NO hashtags and NO @credit inside (appended separately).\n"
        "- hook: SHORT 2-4 word UPPERCASE teaser true for ANY clip ('WAIT FOR IT', "
        "'CAUGHT ON STREAM'), ASCII only. (Often unused; keep it safe/generic.)\n"
        f"- hashtags: 4-5 niche lowercase tags for this game like: {tags_pool}. No "
        "mega-generic tags (#gaming #viral #fyp).\n"
        "- comment_question: ONE short, engaging question to pin as the first "
        "comment (comments drive reach). Generic — e.g. 'Rate this 1-10 👇' or "
        "'Could you pull this off?'. No specific claims."
    )
    cap_strat = strategy.get("caption_strategy") or ""
    learnings = strategy.get("learnings") or ""
    if cap_strat or learnings:
        system += ("\n\nADVISORY (from performance data — use to improve the copy; "
                   "the safety rules above still win):\n")
        if cap_strat:
            system += f"- caption strategy: {cap_strat}\n"
        if learnings:
            system += f"- learnings: {learnings}\n"

    if chosen_index is not None:
        system += (f"\n\nOVERRIDE: the owner has ALREADY chosen clip index {chosen_index}. "
                   f"Set pick_index to exactly {chosen_index} and write the copy for THAT clip only.")
        if human_desc:
            system += (f" The owner WATCHED it and describes the moment as: \"{human_desc}\". "
                       "You MAY write a SPECIFIC, punchy caption about that exact moment — this "
                       "overrides the 'stay general / never name events' rule, but ONLY for facts "
                       "in the owner's description; do not invent anything beyond it.")
    payload = {"game": game_name, "region": slot["region"], "candidates": cands}
    if chosen_index is not None:
        payload["chosen_index"] = chosen_index
        if human_desc:
            payload["owner_description"] = human_desc
    user = json.dumps(payload, ensure_ascii=False)
    body = {"model": CLAUDE_MODEL, "max_tokens": 700, "system": system,
            "messages": [{"role": "user",
                          "content": "Pick the best clip and write the JSON:\n" + user}]}
    try:
        resp = http("POST", "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": env("ANTHROPIC_API_KEY"),
                             "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json=body, timeout=90)
        resp.raise_for_status()
        text = "".join(b.get("text", "") for b in resp.json().get("content", [])
                       if b.get("type") == "text").strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.lstrip().startswith("json"):
                text = text.lstrip()[4:]
            text = text.strip()
        ai = json.loads(text)
        if not isinstance(ai, dict):
            raise ValueError("response was not a JSON object")
        return ai
    except Exception as e:
        print(f"  (Claude judge/caption failed: {e}; top candidate + fallback copy)")
        top = pool[0] if pool else {}
        title = top.get("title") or "Insane clip"
        return {"pick_index": 0, "hook": _clean_drawtext(title),
                "caption": f"{title} — {game_name}",
                "hashtags": _game_hashtags(strategy, slot["game"])[:5],
                "comment_question": "Rate this clip 1-10 👇"}


def assemble_caption(ai, clip, slot, strategy):
    body = (ai.get("caption") or clip.get("title") or "").strip()
    if clip.get("source") == "reddit":
        credit = f"🎥 via r/{clip.get('subreddit')} · u/{clip.get('author')}"
    else:
        broadcaster = clip.get("broadcaster_name")
        clipper = clip.get("creator_name")
        credit = f"🎥 clip: {broadcaster}" if broadcaster else ""
        if clipper and clipper != broadcaster:
            credit += f" (clipped by {clipper})"
    cta = strategy.get("cta", "Follow for daily clips")
    tags = [h for h in (ai.get("hashtags") or []) if isinstance(h, str)][:MAX_HASHTAGS]
    if not tags:
        tags = _game_hashtags(strategy, slot["game"])[:MAX_HASHTAGS]
    parts = [body, credit, cta]
    if DISCLOSURE:
        parts.append(DISCLOSURE)
    parts.append(" ".join(tags))
    caption = "\n\n".join(p for p in parts if p).strip()[:MAX_CAPTION_CHARS]
    return caption, tags


# =============================================================================
# Instagram Reels publish + R2 hosting
# =============================================================================
_IG_UID = None


def _graph_node(node):
    return f"{GRAPH_HOST}/{GRAPH_VERSION}/{node}"


def resolve_ig_user_id(token):
    global _IG_UID
    if os.environ.get("IG_USER_ID"):
        return os.environ["IG_USER_ID"]
    if _IG_UID:
        return _IG_UID
    r = http("GET", _graph_node("me"),
             params={"fields": "user_id,username", "access_token": token}, timeout=60)
    r.raise_for_status()
    uid = r.json().get("user_id")
    if not uid:
        sys.exit(f"Could not resolve IG user_id from token: {r.text}")
    _IG_UID = str(uid)
    return _IG_UID


def _wait_for_container(container_id, token, max_wait=420, interval=10):
    waited = 0
    while waited < max_wait:
        r = http("GET", _graph_node(container_id),
                 params={"fields": "status_code,status", "access_token": token},
                 timeout=60)
        r.raise_for_status()
        st = r.json().get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            sys.exit(f"Reel container {st}: {r.text}")
        time.sleep(interval)
        waited += interval
    sys.exit(f"Reel container not FINISHED after {max_wait}s")


def post_reel_to_instagram(video_url, caption, cover_url=None, thumb_offset=None):
    """Publish a Reel. Prefers a branded cover_url (#3); if IG rejects it, retries
    once with thumb_offset (a frame index). Returns (media_id, cover_method)."""
    token = env("IG_ACCESS_TOKEN")
    ig = resolve_ig_user_id(token)
    base = _graph_node(ig)

    def _create(with_cover):
        d = {"media_type": "REELS", "video_url": video_url, "caption": caption,
             "share_to_feed": "true", "access_token": token}
        if with_cover and cover_url:
            d["cover_url"] = cover_url
        elif thumb_offset is not None:
            d["thumb_offset"] = str(int(thumb_offset))
        return http("POST", f"{base}/media", data=d, timeout=120)

    method = "cover_url" if cover_url else ("thumb_offset" if thumb_offset is not None
                                            else "auto")
    create = _create(True)
    if create.status_code >= 400 and cover_url:
        print(f"  cover_url rejected ({create.status_code}: {create.text[:160]}) — "
              "retrying with thumb_offset")
        method = "thumb_offset" if thumb_offset is not None else "auto"
        create = _create(False)
    create.raise_for_status()
    cid = create.json().get("id")
    if not cid:
        sys.exit(f"No reel container id returned: {create.text}")
    _wait_for_container(cid, token)
    pub = http("POST", f"{base}/media_publish",
               data={"creation_id": cid, "access_token": token}, timeout=120)
    pub.raise_for_status()
    return pub.json().get("id"), method


def post_comment(media_id, message):
    """Pin-worthy first comment (#4). Non-fatal — needs the manage-comments scope on
    the token; if it 400s we log a hint and continue (the Reel already published)."""
    token = env("IG_ACCESS_TOKEN")
    try:
        r = http("POST", _graph_node(f"{media_id}/comments"),
                 data={"message": message, "access_token": token}, timeout=60)
        if r.status_code >= 400:
            print(f"  first comment failed ({r.status_code}): {r.text[:180]}")
            print("  (if this is a permissions error, regenerate CK_IG_ACCESS_TOKEN "
                  "with the instagram_business_manage_comments scope)")
            return False
        return True
    except Exception as e:
        print(f"  first comment error: {e}")
        return False


def r2_configured():
    return all(os.environ.get(k) for k in R2_ENV)


def _r2_client():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=f"https://{env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
        aws_access_key_id=env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def host_file_r2(local_path, key, content_type):
    with open(local_path, "rb") as f:
        _r2_client().put_object(Bucket=env("R2_BUCKET"), Key=key,
                                Body=f.read(), ContentType=content_type)
    return f"{env('R2_PUBLIC_BASE_URL').rstrip('/')}/{key}"


def delete_from_r2(keys):
    if not keys:
        return
    try:
        client, bucket = _r2_client(), env("R2_BUCKET")
        for k in keys:
            client.delete_object(Bucket=bucket, Key=k)
    except Exception as e:
        print(f"(R2 cleanup skipped: {e})")


def sweep_r2_orphans(prefixes=("previews/", "reels/", "covers/", "review/"), older_than_h=6):
    """#22c housekeeping: delete stray R2 objects — dry-run previews, or reel/cover
    objects a crashed run failed to clean up. A live post deletes its objects within
    seconds, so anything older than a few hours is an orphan. Best-effort, never
    fatal, skips silently if R2 isn't configured."""
    if not r2_configured():
        return
    try:
        client, bucket = _r2_client(), env("R2_BUCKET")
        cutoff = now_utc() - datetime.timedelta(hours=older_than_h)
        removed = 0
        for pref in prefixes:
            token = None
            while True:
                kw = {"Bucket": bucket, "Prefix": pref, "MaxKeys": 1000}
                if token:
                    kw["ContinuationToken"] = token
                resp = client.list_objects_v2(**kw)
                for obj in resp.get("Contents", []):
                    lm = obj.get("LastModified")
                    if lm is not None and lm.astimezone(datetime.timezone.utc) < cutoff:
                        client.delete_object(Bucket=bucket, Key=obj["Key"])
                        removed += 1
                if resp.get("IsTruncated"):
                    token = resp.get("NextContinuationToken")
                else:
                    break
        if removed:
            print(f"  R2 sweep: removed {removed} orphaned object(s).")
    except Exception as e:
        print(f"  (R2 sweep skipped: {e})")


def _safe_key(s):
    return "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in str(s))


# =============================================================================
# YouTube Shorts cross-post (optional — only fires if YT_* secrets are present)
# =============================================================================
def crosspost_youtube(reel_path, strategy, slot, title_base, credit, tags):
    """Push the same reel to YouTube as a Short. Non-fatal: any failure is logged
    and returns None so it can never break the IG flow. Skips silently unless the
    YT_* secrets are set and strategy.youtube.enabled isn't false."""
    if not youtube.configured():
        return None
    if not (strategy.get("youtube", {}) or {}).get("enabled", True):
        return None
    game_name = strategy["games"].get(slot["game"], slot.get("game", "")) if slot.get("game") else ""
    title = f"{title_base} | {game_name} #Shorts" if game_name else f"{title_base} #Shorts"
    description = "\n\n".join(p for p in [
        title_base,
        f"🎥 Clip credit: {credit}" if credit else "",
        "Daily best-of gaming clips — subscribe for more 🎮",
        " ".join(tags or []),
    ] if p)
    yt_tags = [t.lstrip("#") for t in (tags or [])]
    if game_name:
        yt_tags.append(game_name.lower())
    yt_tags += ["gaming", "clips", "shorts"]
    privacy = (strategy.get("youtube", {}) or {}).get("privacy", "public")
    try:
        vid = youtube.upload_short(reel_path, title=title, description=description,
                                   tags=yt_tags, privacy=privacy)
        vid_id = vid.get("id")
        status = (vid.get("status") or {}).get("privacyStatus")
        url = f"https://youtu.be/{vid_id}" if vid_id else None
        print(f"  YouTube: uploaded {url} (privacy={status})")
        if status == "private" and privacy != "private":
            print("  NOTE: YouTube forced this PRIVATE — the Cloud project needs the "
                  "YouTube API compliance audit for public posting. Flip it in Studio, "
                  "or submit the audit form for hands-off public Shorts.")
        return {"id": vid_id, "url": url, "privacy": status}
    except Exception as e:
        print(f"  YouTube cross-post failed (non-fatal): {str(e)[:200]}")
        return None


# =============================================================================
# Weekly Top-3 compilation (#8)
# =============================================================================
def _recent_top_posts(history, days=7, n=3):
    """The best real (non-compilation) posts in the last `days`, ranked by reach/
    views. Used to build the weekly Top-3."""
    cutoff = now_utc() - datetime.timedelta(days=days)

    def perf(p):
        m = p.get("metrics") or {}
        return m.get("views") or m.get("reach") or p.get("clip_views") or 0

    elig = []
    for p in history.get("posts", []):
        if p.get("format") == "top3" or not p.get("clip_url"):
            continue
        t = _parse_ts(p.get("date_utc"))
        if t and t >= cutoff:
            elig.append(p)
    elig.sort(key=perf, reverse=True)
    return elig[:n]


def _concat_reels(segments, out_path):
    """Join N already-normalised 9:16 segments into one reel. Re-encodes via the
    concat filter so minor timing differences still join cleanly. Assumes each
    segment has an audio track (Twitch clips do); fails loudly otherwise."""
    _ensure_tool("ffmpeg")
    inputs = []
    for s in segments:
        inputs += ["-i", s]
    n = len(segments)
    streams = "".join(f"[{i}:v][{i}:a]" for i in range(n))
    graph = f"{streams}concat=n={n}:v=1:a=1[v][a]"
    cmd = (["ffmpeg", "-y"] + inputs
           + ["-filter_complex", graph, "-map", "[v]", "-map", "[a]",
              "-c:v", "libx264", "-preset", "slow", "-crf", "18",
              "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
              "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path])
    proc = subprocess.run(cmd, timeout=420, stderr=subprocess.PIPE)
    if proc.returncode != 0 or not os.path.exists(out_path):
        sys.exit("ffmpeg concat failed:\n"
                 + proc.stderr.decode("utf-8", "replace")[-1500:])
    return out_path


def post_top3(strategy, history, dry=False):
    """#8 weekly compilation: re-cut the week's top-3 clips (by reach/views) into a
    single ranked '#1/#2/#3' reel — a recap that rewards the audience and gives the
    grid a distinct weekly beat. Idempotent per day via a 'top3-<date>' slot_key."""
    tkey = "top3-" + local_now(strategy).date().isoformat()
    if any(p.get("slot_key") == tkey for p in history.get("posts", [])):
        print("Top-3 already posted today — skipping.")
        return
    picks = _recent_top_posts(history, days=int(strategy.get("top3_days", 7)), n=3)
    if len(picks) < 3:
        print(f"Top-3: only {len(picks)} eligible posts in the window — skipping "
              "(need 3).")
        return

    tmp = tempfile.gettempdir()
    segments, credits, games = [], [], []
    for rank, p in enumerate(picks, start=1):
        clip = {"id": p["clip_id"], "url": p.get("clip_url"),
                "source": p.get("source", "twitch")}
        raw = os.path.join(tmp, f"ck_t3_{rank}_{_safe_key(p['clip_id'])}.mp4")
        try:
            download_clip(clip, raw)
        except Exception as e:
            tail = (str(e).splitlines() or [""])[-1][:120]
            print(f"  top3 #{rank}: download failed ({p.get('clip_id')}): {tail}; skip")
            continue
        dur = _probe(raw).get("duration_s")
        win = _audio_peak_window(raw, dur, 4.0, 6.0,
                                 float(strategy.get("min_duration_s", 5)))
        seg = os.path.join(tmp, f"ck_t3seg_{rank}.mp4")
        seg_slot = {"game": p.get("game"), "region": p.get("region")}
        reformat_reel(raw, seg, strategy, seg_slot, trim=win,
                      max_s=int(strategy.get("top3_seg_s", 10)),
                      rank_label=f"#{rank}", cta=False)
        segments.append(seg)
        credits.append(p.get("broadcaster") or p.get("author") or "a creator")
        games.append(p.get("game"))
    if len(segments) < 2:
        print("Top-3: fewer than 2 usable segments after downloads — skipping.")
        return

    comp = os.path.join(tmp, "ck_top3.mp4")
    _concat_reels(segments, comp)
    duration_s = _probe(comp).get("duration_s")
    print(f"Built Top-3 reel: {comp} ({os.path.getsize(comp) // 1024} KB, {duration_s}s)")

    uniq_games = list(dict.fromkeys(games))
    label = " + ".join(strategy["games"].get(g, g) for g in uniq_games) or "gaming"
    credit_line = " · ".join(f"#{i + 1} {c}" for i, c in enumerate(credits))
    tags = []
    for g in uniq_games:
        tags += _game_hashtags(strategy, g)
    tags = list(dict.fromkeys(tags))[:MAX_HASHTAGS]
    caption = "\n\n".join([
        f"TOP 3 {label} clips of the week 🏆 which one is your #1?",
        f"🎥 {credit_line}",
        strategy.get("cta", "Follow @clipkroniek for daily clips 🎮"),
        " ".join(tags),
    ]).strip()[:MAX_CAPTION_CHARS]
    print("Top-3 caption:\n" + caption)

    cover_path = build_cover(segments[0], os.path.join(tmp, "ck_top3_cover.jpg"),
                             strategy, {"game": games[0]}, history, 0.5)

    if dry:
        print("\nDRY_RUN=1 — built the Top-3 reel; not publishing.")
        if r2_configured():
            print("Preview: " + host_file_r2(comp, "previews/ck_top3.mp4", "video/mp4"))
        return

    if not r2_configured():
        sys.exit("Reels need R2 configured (a public video_url).")
    stamp = now_utc().strftime("%Y%m%d")
    r2_key = f"reels/ck_top3_{stamp}.mp4"
    video_url = host_file_r2(comp, r2_key, "video/mp4")
    cover_url, cover_key = None, None
    if cover_path:
        cover_key = f"covers/ck_top3_{stamp}.jpg"
        cover_url = host_file_r2(cover_path, cover_key, "image/jpeg")
    media_id, cover_method = post_reel_to_instagram(
        video_url, caption, cover_url=cover_url, thumb_offset=500)
    print(f"Published Top-3 reel — media_id={media_id} (cover={cover_method})")

    # --- pinned first comment (#4) — same engagement play as the daily posts ------
    first_comment = (f"Which clip gets your #1 vote? 👇\n\n"
                     f"Best-of {label} clips every day — drop a follow 🎮")
    if not post_comment(media_id, first_comment):
        first_comment = None

    yt = None
    if youtube.configured():
        yt = crosspost_youtube(comp, strategy, {"game": None, "region": "mixed"},
                               f"Top 3 {label} clips of the week", credit_line, tags)

    delete_from_r2([k for k in (r2_key, cover_key) if k])

    local = local_now(strategy)
    history["posts"].append({
        "slot_key": tkey,
        "date_utc": now_utc().isoformat(),
        "local_date": local.date().isoformat(),
        "weekday": local.weekday(),
        "slot_hour": local.hour,
        "game": "+".join(uniq_games),
        "region": "mixed",
        "source": "compilation",
        "format": "top3",
        "clip_id": tkey,
        "clip_ids": [p["clip_id"] for p in picks],
        "duration_s": round(float(duration_s), 2) if duration_s else None,
        "cover": cover_method,
        "first_comment": first_comment,
        "youtube": yt,
        "hashtags": tags,
        "media_id": media_id,
        "metrics": {},
        "measured_at": None,
    })
    save_history(history)
    print("Recorded Top-3 to history.json")


# =============================================================================
# Human-in-the-loop review (Telegram) — propose top-3 -> owner picks -> fulfill
# =============================================================================
PENDING_PREFIX = "review/pending-"


def _pending_key(slot_key):
    return f"{PENDING_PREFIX}{_safe_key(slot_key)}.json"


def _save_pending(state):
    _r2_client().put_object(Bucket=env("R2_BUCKET"), Key=_pending_key(state["key"]),
                            Body=json.dumps(state, ensure_ascii=False).encode("utf-8"),
                            ContentType="application/json")


def _load_pending(key):
    try:
        b = _r2_client().get_object(Bucket=env("R2_BUCKET"), Key=key)["Body"].read()
        return json.loads(b.decode("utf-8"))
    except Exception:
        return None


def _clear_pending(state):
    try:
        _r2_client().delete_object(Bucket=env("R2_BUCKET"), Key=_pending_key(state["key"]))
    except Exception as e:
        print(f"  (pending clear skipped: {e})")


def _list_pending_keys():
    keys, token = [], None
    client, bucket = _r2_client(), env("R2_BUCKET")
    while True:
        kw = {"Bucket": bucket, "Prefix": PENDING_PREFIX, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        resp = client.list_objects_v2(**kw)
        keys += [o["Key"] for o in resp.get("Contents", []) if o["Key"].endswith(".json")]
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
        else:
            break
    return keys


def _pending_exists():
    try:
        return bool(_list_pending_keys())
    except Exception:
        return False


def _posted_key(slot_key):
    return f"review/posted-{_safe_key(slot_key)}.json"


def _mark_posted(slot_key, media_id):
    """Durable 'this slot was published' flag in R2, written at publish time. Because it
    does NOT depend on the git push landing history.json, it stops a lost/failed push from
    letting the next cron re-propose (and thus double-post) the same slot."""
    try:
        _r2_client().put_object(
            Bucket=env("R2_BUCKET"), Key=_posted_key(slot_key),
            Body=json.dumps({"slot_key": slot_key, "media_id": media_id,
                             "posted_utc": now_utc().isoformat()}).encode("utf-8"),
            ContentType="application/json")
    except Exception as e:
        print(f"  (posted-marker write skipped: {e})")


def _already_posted(slot_key):
    try:
        _r2_client().get_object(Bucket=env("R2_BUCKET"), Key=_posted_key(slot_key))
        return True
    except Exception:
        return False                    # 404 (or transient) -> treat as not-yet-posted


def propose_review(strategy, history, slot, key, pool):
    """PROPOSE step: send the top candidates to the owner's phone and stash a pending
    decision in R2. The fulfill step posts the owner's pick (or auto-picks after the
    window). ANY failure falls back to an autonomous post now, so a slot is never lost."""
    hr = strategy.get("human_review", {}) or {}
    ncand = max(2, int(hr.get("candidates", 3)))
    window = int(hr.get("respond_window_min", 30))
    if not r2_configured():
        print("  human-review needs R2 — falling back to autonomous.")
        return _produce_and_post(strategy, history, slot, key, pool)
    if _already_posted(key):
        print("  slot already posted (durable marker) — skipping (guards a lost git push).")
        return
    if _pending_exists():
        print("  a review is already pending — skipping this slot to avoid overlap.")
        return

    tmp = tempfile.gettempdir()
    game_label = strategy["games"].get(slot["game"], slot["game"])
    cands, previews, preview_keys = [], [], []
    for c in pool:
        if len(cands) >= ncand:
            break
        rawp = os.path.join(tmp, f"ck_rev_{_safe_key(c['id'])}.mp4")
        try:
            download_clip(c, rawp)
            pkey = f"review/{_safe_key(c['id'])}.mp4"
            url = host_file_r2(rawp, pkey, "video/mp4")
        except Exception as e:
            print(f"  review preview failed ({c.get('id')}): "
                  f"{(str(e).splitlines() or [''])[-1][:100]}")
            continue
        cands.append(c)
        preview_keys.append(pkey)
        previews.append({"n": len(cands), "path": rawp, "url": url, "title": c.get("title"),
                         "broadcaster": c.get("broadcaster_name"), "game": game_label,
                         "views": c.get("view_count")})

    if len(cands) < 2:
        print("  <2 previewable candidates — falling back to autonomous post.")
        delete_from_r2(preview_keys)
        return _produce_and_post(strategy, history, slot, key, pool)

    baseline = telegram.send_candidates(previews, f"{game_label} {slot.get('region', '')}".strip())
    if baseline is None:
        print("  Telegram send failed — falling back to autonomous post.")
        delete_from_r2(preview_keys)
        return _produce_and_post(strategy, history, slot, key, pool)

    state = {"key": key, "slot": slot, "pool": cands, "preview_keys": preview_keys,
             "baseline_update_id": baseline,
             "deadline_utc": (now_utc() + datetime.timedelta(minutes=window)).isoformat(),
             "created_utc": now_utc().isoformat()}
    try:
        _save_pending(state)
    except Exception as e:
        print(f"  could not save pending ({e}) — falling back to autonomous post.")
        delete_from_r2(preview_keys)
        return _produce_and_post(strategy, history, slot, key, pool)
    print(f"  proposed {len(cands)} clips to Telegram; {window}m window "
          f"(deadline {state['deadline_utc']}).")


def fulfill_reviews(strategy, history, dry=False):
    """FULFILL step (own cron): read the owner's Telegram reply and post their pick, or
    auto-post once the window has passed. Idempotent per slot_key; at most one pending."""
    if not (strategy.get("human_review", {}) or {}).get("enabled"):
        return
    if not (r2_configured() and telegram.configured()):
        return
    for pkey in _list_pending_keys():
        state = _load_pending(pkey)
        if not state:                                 # unreadable/corrupt -> GC so it can't wedge
            try:
                _r2_client().delete_object(Bucket=env("R2_BUCKET"), Key=pkey)
            except Exception:
                pass
            continue
        slot_key = state.get("key")
        if _already_posted(slot_key) or any(p.get("slot_key") == slot_key
                                            for p in history.get("posts", [])):
            _clear_pending(state)                     # already posted -> stale pending
            delete_from_r2(state.get("preview_keys", []))
            continue
        pool = state.get("pool") or []
        choice, desc, last = telegram.poll_decision(state.get("baseline_update_id", 0), len(pool))
        deadline = _parse_ts(state.get("deadline_utc"))
        chosen = human_desc = None
        if choice is not None:
            chosen, human_desc = choice - 1, desc
            telegram.send_message(f"✅ Posting #{choice}" + (f" — {desc}" if desc else "") + " now …")
        elif deadline and now_utc() >= deadline:
            telegram.send_message("⏰ No reply in the window — auto-posting my pick.")
            # chosen stays None -> Claude picks among the candidates (autonomous)
        else:
            state["baseline_update_id"] = last        # remember progress, keep waiting
            _save_pending(state)
            continue
        try:
            _produce_and_post(strategy, history, state["slot"], slot_key, pool,
                              dry=dry, chosen_index=chosen, human_desc=human_desc)
        except (Exception, SystemExit) as e:          # leave pending -> retry / deadline fallback
            print(f"  fulfill: post attempt failed ({e}) — keeping pending for retry.")
            telegram.send_message("⚠️ That post attempt failed; I'll retry shortly.")
            continue
        if not dry:                                   # tear down only after a real successful post
            delete_from_r2(state.get("preview_keys", []))
            _clear_pending(state)


# =============================================================================
# Orchestration
# =============================================================================
def main():
    strategy = load_strategy()
    history = load_history()
    dry = os.environ.get("DRY_RUN") == "1"
    discover_only = os.environ.get("DISCOVER_ONLY") == "1"
    force = os.environ.get("FORCE") == "1"
    # REVIEW_FORCE: bypass the SCHEDULE like FORCE, but still go through the Telegram
    # review (FORCE alone posts autonomously). Used to test/run the human-in-the-loop
    # flow on demand.
    review_force = os.environ.get("REVIEW_FORCE") == "1"

    if os.environ.get("REVIEW_PING") == "1":     # one-off Telegram connectivity check
        if telegram.configured():
            ok = telegram.send_message(
                "✅ Clipkroniek connected. At each slot I'll send you the top 3 clips — "
                "reply like:  2 - insane 1v4 clutch  (number = your pick, rest = a one-line "
                "hint for the caption). No reply in 30 min → I auto-post so nothing's missed.")
            print("telegram ping:", "sent ✓" if ok.get("ok") else "FAILED (check the token/chat_id)")
        else:
            print("telegram not configured — TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing.")
        return

    if os.environ.get("REVIEW_FULFILL") == "1":
        fulfill_reviews(strategy, history, dry=dry)
        return

    if not (dry or discover_only):
        sweep_r2_orphans()          # #22c: clean up any crash-orphaned R2 objects

    if os.environ.get("FORMAT_OVERRIDE") == "top3":
        print(f"[{BRAND_NAME}] FORMAT_OVERRIDE=top3 — weekly compilation run.")
        post_top3(strategy, history, dry=dry)
        return

    if os.environ.get("FORMAT_OVERRIDE") == "longform":
        print(f"[{BRAND_NAME}] FORMAT_OVERRIDE=longform — weekly long-form run.")
        import longform          # late import: dormant unless triggered
        longform.run(strategy, history, dry=dry)
        return

    if force or review_force:
        slot, key = forced_slot(strategy)
        print(f"{'REVIEW_FORCE' if review_force else 'FORCE'}=1 — bypassing the schedule "
              f"for this run{' (still goes through Telegram review)' if review_force else ''}.")
    else:
        slot, key = find_due_slot(strategy, history)

    if slot is None:
        local = local_now(strategy)
        print(f"[{BRAND_NAME}] {local.strftime('%a %Y-%m-%d %H:%M %Z')} — "
              "not a scheduled slot (or already posted / daily cap reached). "
              "Nothing to do.")
        return

    slot = dict(slot)
    for envk, field in (("SLOT_GAME", "game"), ("SLOT_REGION", "region"),
                        ("SLOT_SOURCE", "source")):
        if os.environ.get(envk):
            slot[field] = os.environ[envk]
    print(f"[{BRAND_NAME}] slot {key} — game={slot['game']} "
          f"region={slot['region']} source={slot.get('source', 'twitch')}")

    pool = discover_clip(strategy, slot, history)
    if not pool:
        print("No fresh qualifying clip for this slot — skipping (no post).")
        return

    hr = strategy.get("human_review", {}) or {}
    if hr.get("enabled") and telegram.configured() and not (force or dry or discover_only):
        propose_review(strategy, history, slot, key, pool)
        return
    _produce_and_post(strategy, history, slot, key, pool, dry=dry,
                      discover_only=discover_only)


def _produce_and_post(strategy, history, slot, key, pool, *, dry=False,
                      discover_only=False, human_desc=None, chosen_index=None):
    """Build and publish ONE post from `pool`. Normally Claude picks; in the human-in-
    the-loop path `chosen_index` (the owner's pick) is forced and `human_desc` (what the
    owner said the clip is about) grounds a sharper caption."""
    # #7: let Claude judge the metadata and pick; fall back to the top-ranked clip.
    ai = call_claude(pool, slot, strategy, human_desc=human_desc, chosen_index=chosen_index)
    pick = chosen_index if chosen_index is not None else ai.get("pick_index", 0)
    try:
        pick = int(pick)
        if not (0 <= pick < len(pool)):
            pick = 0
    except Exception:
        pick = 0
    if ai.get("why_pick"):
        print(f"  judge picked #{pick}: {str(ai.get('why_pick'))[:120]}")
    ordered = [pool[pick]] + [c for i, c in enumerate(pool) if i != pick]
    clip = ordered[0]

    caption, tags = assemble_caption(ai, clip, slot, strategy)
    burn = bool(strategy.get("burn_hook", False))
    hook = (ai.get("hook") or "").strip() if burn else ""
    print(f"Picked: {clip.get('view_count')} views @ {clip.get('velocity')}/h | "
          f"lang={clip.get('language')} | {(clip.get('title') or '')[:60]}")
    print(f"  by {clip.get('broadcaster_name')} — {clip.get('url')}")
    print("Caption:\n" + caption)

    if discover_only:
        print("\nDISCOVER_ONLY=1 — stopping before download/build/publish.")
        return

    tmp = tempfile.gettempdir()
    # --- download with fallback across the ranked pool (#22a) ------------
    raw = None
    for cand in ordered[:6]:
        cand_raw = os.path.join(tmp, f"ck_raw_{_safe_key(cand['id'])}.mp4")
        try:
            print(f"Downloading clip {cand.get('id')} ...")
            download_clip(cand, cand_raw)
            clip, raw = cand, cand_raw
            break
        except Exception as e:
            tail = (str(e).splitlines() or [""])[-1][:160]
            print(f"  download failed ({cand.get('id')}): {tail}; trying next")
    if raw is None:
        sys.exit("All candidate downloads failed for this slot.")
    if clip is not ordered[0]:                    # fallback changed the clip -> recaption
        if human_desc or chosen_index is not None:  # owner's description no longer applies
            ai = call_claude([clip], slot, strategy)
        caption, tags = assemble_caption(ai, clip, slot, strategy)

    dur_src = _probe(raw).get("duration_s")
    # --- smart-trim (#12): audio peak + transcript-assisted cut moment ---------
    trim, trimmed, trim_meta, transcript = _decide_trim(strategy, key, raw, dur_src)
    if trim:
        src = ("audio+speech" if trim_meta.get("transcript_used")
               else "action/no-speech" if trim_meta.get("no_speech") else "audio")
        print(f"  smart-trim ON [{src}]: {trim[0]}-{trim[1]}s (cut @ {trim[2]}s of "
              f"{dur_src}s, lead-in {trim_meta.get('lead_in_s')}s + after {int(strategy.get('smart_trim',{}).get('post_s',5))}s)")
        if trim_meta.get("verbal_moment") is not None:
            print(f"    transcript ({trim_meta.get('language')}): reaction @ "
                  f"{trim_meta.get('verbal_moment')}s vs audio-peak "
                  f"{trim_meta.get('audio_peak')}s")
    else:
        print(f"  smart-trim: off this post — full clip ({dur_src}s)")

    # --- facecam detection (Claude Vision, one frame) -------------------
    # Streamer reactions are half the clip; the zoom-crop was cutting the corner
    # facecam off. Detect it and STACK it above the gameplay. None -> standard layout.
    facecam = None
    if (strategy.get("facecam_stack", {}) or {}).get("enabled", True) and vision.configured():
        at = trim[2] if trim else (float(dur_src) * 0.4 if dur_src else 1.0)
        frame = os.path.join(tmp, f"ck_frame_{_safe_key(clip['id'])}.jpg")
        if _extract_frame(raw, at, frame):
            fc = vision.detect_facecam(frame)
            if fc:
                facecam = fc["box"]
                print(f"  facecam: {fc.get('corner')} "
                      f"box={['%.2f' % v for v in facecam]} (conf {fc.get('confidence')})"
                      " -> STACKED layout")
            else:
                print("  facecam: none detected -> standard layout")

    # --- animated captions (from the transcript we already have) + credit ------
    reel_max = int(strategy.get("max_duration_s", 60))
    if trim:
        reel_dur = min(trim[1] - trim[0], reel_max)
        cap_offset = trim[0]
    else:
        reel_dur = min(float(dur_src), reel_max) if dur_src else reel_max
        cap_offset = 0.0
    captions_ass = None
    translated = False
    cap_cfg = strategy.get("captions", {}) or {}
    if cap_cfg.get("enabled", True) and transcript and transcript.get("words"):
        ass_name = "ck_captions.ass"          # written in cwd (clipkroniek); gitignored
        en_seg = transcript.get("en_segments") if cap_cfg.get("translate", True) else None
        if captions.build_ass(transcript["words"], os.path.join(os.getcwd(), ass_name),
                              reel_dur, language=transcript.get("language"),
                              offset=cap_offset,
                              font_size=int(cap_cfg.get("font_size", 80)),
                              pos_y=int(cap_cfg.get("pos_y", 1180)),
                              upper=bool(cap_cfg.get("uppercase", True)),
                              translation=en_seg,
                              trans_font_size=int(cap_cfg.get("translate_font_size", 52)),
                              trans_pos_y=int(cap_cfg.get("translate_pos_y", 1330))):
            captions_ass = ass_name
            translated = bool(en_seg)
            print(f"  captions: {len(transcript['words'])} words -> animated "
                  f"({transcript.get('language')})"
                  + (f" + {len(en_seg)} EN translation lines" if en_seg else ""))
    credit = _streamer_credit(clip)

    reel = os.path.join(tmp, f"ck_reel_{_safe_key(clip['id'])}.mp4")
    print("Reformatting to 9:16 Reel...")
    reformat_reel(raw, reel, strategy, slot, hook_text=hook, trim=trim,
                  max_s=reel_max, facecam=facecam, credit=credit,
                  captions_ass=captions_ass)
    r_dur = _probe(reel).get("duration_s")
    duration_s = r_dur or (trim[1] - trim[0] if trim else dur_src)
    print(f"Built reel: {reel} ({os.path.getsize(reel) // 1024} KB, {duration_s}s)")

    # --- branded cover (#3): frame at the action peak (or 40%) ----------
    cover_at = trim[2] if trim else (float(dur_src) * 0.4 if dur_src else 1.0)
    cover_path = None
    if (strategy.get("cover", {}) or {}).get("enabled", True):
        cover_path = build_cover(
            raw, os.path.join(tmp, f"ck_cover_{_safe_key(clip['id'])}.jpg"),
            strategy, slot, history, cover_at)

    if dry:
        print("\nDRY_RUN=1 — built the reel; not publishing.")
        if r2_configured():
            purl = host_file_r2(reel, f"previews/{_safe_key(clip['id'])}.mp4", "video/mp4")
            print(f"Preview (hosted, NOT posted): {purl}")
            if cover_path:
                curl = host_file_r2(cover_path, f"previews/{_safe_key(clip['id'])}.jpg",
                                    "image/jpeg")
                print(f"Cover preview: {curl}")
        else:
            print("(R2 not configured — no preview URL; file is on the runner only.)")
        return

    if not r2_configured():
        sys.exit("Reels need R2 configured (a public video_url).")
    r2_key = f"reels/{_safe_key(clip['id'])}.mp4"
    video_url = host_file_r2(reel, r2_key, "video/mp4")
    print(f"Hosted: {video_url}")
    cover_url, cover_key = None, None
    if cover_path:
        cover_key = f"covers/{_safe_key(clip['id'])}.jpg"
        cover_url = host_file_r2(cover_path, cover_key, "image/jpeg")
        print(f"Cover: {cover_url}")
    # thumb_offset fallback (ms into the FINAL reel): peak relative to trim start.
    thumb_ms = int(max(0.0, cover_at - (trim[0] if trim else 0.0)) * 1000)
    media_id, cover_method = post_reel_to_instagram(
        video_url, caption, cover_url=cover_url, thumb_offset=thumb_ms)
    print(f"Published reel — media_id={media_id} (cover={cover_method})")
    # Durable 'this slot is posted' flag (survives a lost git push) — review mode only.
    if (strategy.get("human_review", {}) or {}).get("enabled") and r2_configured():
        _mark_posted(key, media_id)

    # --- pinned first comment (#4) --------------------------------------
    first_comment = None
    cq = (ai.get("comment_question") or "").strip()
    if cq:
        n = len(history.get("posts", [])) + 1
        first_comment = f"{cq}\n\nDaily best-of gaming clips — drop a follow 🎮 (#{n})"
        if not post_comment(media_id, first_comment):
            first_comment = None

    # --- YouTube Shorts cross-post (optional; reuses the same reel) ------
    yt = None
    if youtube.configured():
        title_base = ((caption.split("\n", 1)[0].strip() if caption else "")
                      or clip.get("title") or "Insane gaming clip")
        credit = clip.get("broadcaster_name") or clip.get("author")
        yt = crosspost_youtube(reel, strategy, slot, title_base, credit, tags)

    delete_from_r2([k for k in (r2_key, cover_key) if k])

    local = local_now(strategy)
    history["posts"].append({
        "slot_key": key,
        "date_utc": now_utc().isoformat(),
        "local_date": local.date().isoformat(),
        "weekday": int(slot["weekday"]),
        "slot_hour": int(slot["hour"]),
        "game": slot["game"],
        "region": slot["region"],
        "source": clip.get("source", slot.get("source", "twitch")),
        "curated": bool(clip.get("is_curated")),
        "format": "single",
        "subreddit": clip.get("subreddit"),
        "author": clip.get("author"),
        "clip_id": clip["id"],
        "clip_url": clip.get("url"),
        "broadcaster": clip.get("broadcaster_name"),
        "creator": clip.get("creator_name"),
        "language": clip.get("language"),
        "clip_views": clip.get("view_count"),
        "clip_velocity": clip.get("velocity"),
        "duration_s": round(float(duration_s), 2) if duration_s else None,
        "trimmed": bool(trimmed),
        "trim": [trim[0], trim[1]] if trim else None,
        "trim_hint": trim_meta,
        "cover": cover_method,
        "first_comment": first_comment,
        "youtube": yt,
        "facecam": list(facecam) if facecam else None,
        "captions": bool(captions_ass),
        "translated": translated,
        "credit": credit,
        "title": clip.get("title"),
        "hook": hook,
        "hashtags": tags,
        "media_id": media_id,
        "metrics": {},
        "measured_at": None,
    })
    save_history(history)
    print("Recorded to history.json")


if __name__ == "__main__":
    main()
