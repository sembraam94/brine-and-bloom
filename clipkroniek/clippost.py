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


def _discover_twitch(strategy, slot, posted_ids):
    """Candidate list from Twitch: right game, region (by clip language), length,
    not already posted."""
    cid = env("TWITCH_CLIENT_ID")
    secret = env("TWITCH_CLIENT_SECRET")
    token = twitch.get_app_token(cid, secret, http)
    game_name = strategy["games"][slot["game"]]
    game_id = twitch.resolve_game_id(game_name, cid, token, http)

    hours = int(strategy.get("recency_hours", 24))
    ended = now_utc()
    started = ended - datetime.timedelta(hours=hours)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    clips = twitch.get_recent_clips(game_id, started.strftime(fmt),
                                    ended.strftime(fmt), cid, token, http, pages=4)

    langs = _region_langs(strategy, slot["region"])
    min_d = float(strategy.get("min_duration_s", 5))
    max_d = float(strategy.get("max_duration_s", 60))
    base = []
    for cl in clips:
        if cl.get("id") in posted_ids:
            continue
        if (cl.get("language") or "en") not in langs:
            continue
        dur = cl.get("duration")
        if dur is not None and (float(dur) < min_d or float(dur) > max_d):
            continue
        cl["source"] = "twitch"
        base.append(cl)
    label = (f"{game_name} {slot['region']}={sorted(langs)} last {hours}h "
             f"({len(clips)} pulled)")
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


def discover_clip(strategy, slot, history):
    """Best not-yet-posted clip for this slot, from its source (twitch|reddit),
    ranked by view_count (Twitch views / Reddit upvotes)."""
    posted_ids = {p.get("clip_id") for p in history.get("posts", [])}
    min_v = int(strategy.get("min_view_count", 50))
    hard_floor = int(strategy.get("hard_view_floor", 10))
    source = slot.get("source", "twitch")

    if source == "reddit":
        base, label = _discover_reddit(strategy, slot, posted_ids)
    else:
        base, label = _discover_twitch(strategy, slot, posted_ids)

    base.sort(key=lambda cl: int(cl.get("view_count") or 0), reverse=True)
    # Prefer clips above the target floor; else fall back to the best available
    # so the slot still posts (consistency > squeezing the count).
    preferred = [cl for cl in base if int(cl.get("view_count") or 0) >= min_v]
    fallback = [cl for cl in base if int(cl.get("view_count") or 0) >= hard_floor]
    pool = preferred or fallback
    print(f"  discovery[{source}]: {label}; {len(base)} candidates, "
          f"{len(preferred)} >= {min_v}"
          + ("" if preferred else f" -> best-available (>= {hard_floor})"))
    best = pool[0] if pool else None
    if best:
        best.setdefault("source", source)
    return best


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


def _font():
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/Arial.ttf"):
        if os.path.exists(p):
            return p
    return None


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
        sys.exit(f"yt-dlp failed to download after retries:\n{last}")
    return out_path


def _clean_drawtext(s):
    """Sanitize an on-screen hook to a short, ffmpeg-drawtext-safe ASCII string."""
    s = "".join(ch for ch in (s or "") if ch.isalnum() or ch in " !?.-")
    return s.upper().strip()[:26] or "WATCH THIS"


def reformat_reel(in_path, out_path, hook_text, max_s=60):
    """Blurred 9:16 fill + centered gameplay + a bold hook banner near the top."""
    _ensure_tool("ffmpeg")
    hook = _clean_drawtext(hook_text) if hook_text else ""
    font = _font()
    fill = (
        f"[0:v]scale={REEL_W}:{REEL_H}:force_original_aspect_ratio=increase,"
        f"crop={REEL_W}:{REEL_H},boxblur=26:6,eq=brightness=-0.10[bg];"
        f"[0:v]scale={REEL_W}:-2:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[base]"
    )
    if font and hook:                     # burn the hook banner only if enabled + a font exists
        vf = (fill + ";"
              f"[base]drawtext=fontfile='{font}':text='{hook}':fontcolor=white:"
              f"fontsize=68:borderw=6:bordercolor=black@0.9:x=(w-text_w)/2:y=190:"
              f"box=1:boxcolor=black@0.45:boxborderw=26[out]")
        out_label = "[out]"
    else:
        print("  (no font found — skipping the text hook overlay)")
        vf = fill
        out_label = "[base]"
    cmd = ["ffmpeg", "-y", "-i", in_path, "-filter_complex", vf,
           "-map", out_label, "-map", "0:a?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart",
           "-r", "30", "-t", str(max_s), out_path]
    proc = subprocess.run(cmd, timeout=300, stderr=subprocess.PIPE)
    if proc.returncode != 0 or not os.path.exists(out_path):
        sys.exit("ffmpeg reformat failed:\n" + proc.stderr.decode("utf-8", "replace")[-1500:])
    return out_path


# =============================================================================
# Caption (Claude)
# =============================================================================
def call_claude(clip, slot, strategy):
    game_name = strategy["games"][slot["game"]]
    pool = " ".join(GAME_HASHTAGS.get(slot["game"], []))
    system = (
        "You write for 'Clipkroniek', an Instagram page that reposts the best "
        "gaming clips WITH creator credit. You get ONE clip's METADATA (title, "
        "game, streamer) but you CANNOT watch the video. Return a JSON object "
        "ONLY (no prose, no markdown):\n"
        '{"hook": "...", "caption": "...", "hashtags": ["#..."]}\n'
        "CRITICAL: because you can't see the clip, NEVER invent or assert specific "
        "events, outcomes, kill counts, or who-did-what — you will be wrong. Stay "
        "intriguing but GENERAL.\n"
        "- caption: 1-2 short punchy lines. A general curiosity/hype line that "
        "names the game plainly (for search). English. NO hashtags and NO @credit "
        "inside (both are appended separately).\n"
        "- hook: a SHORT 2-4 word UPPERCASE on-screen teaser that is true for ANY "
        "clip (e.g. 'WAIT FOR IT', 'CAUGHT ON STREAM', 'GTA RP MOMENT'), ASCII "
        "only, no specific claims. (Often unused, but keep it safe/generic.)\n"
        f"- hashtags: 4-5 niche tags for this game, lowercase, like: {pool}. "
        "No mega-generic tags (#gaming, #viral, #fyp)."
    )
    user = json.dumps({
        "game": game_name,
        "clip_title": clip.get("title"),
        "broadcaster": clip.get("broadcaster_name"),
        "clipper": clip.get("creator_name"),
        "view_count": clip.get("view_count"),
        "language": clip.get("language"),
    }, ensure_ascii=False)

    body = {"model": CLAUDE_MODEL, "max_tokens": 500, "system": system,
            "messages": [{"role": "user",
                          "content": "Write the JSON for this clip:\n" + user}]}
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
    try:
        return json.loads(text)
    except Exception:
        title = clip.get("title") or "Insane clip"
        return {"hook": title[:24], "caption": f"{title} — {game_name}",
                "hashtags": GAME_HASHTAGS.get(slot["game"], [])[:5]}


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
        tags = GAME_HASHTAGS.get(slot["game"], [])[:MAX_HASHTAGS]
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


def post_reel_to_instagram(video_url, caption):
    token = env("IG_ACCESS_TOKEN")
    ig = resolve_ig_user_id(token)
    base = _graph_node(ig)
    create = http("POST", f"{base}/media",
                  data={"media_type": "REELS", "video_url": video_url,
                        "caption": caption, "share_to_feed": "true",
                        "access_token": token}, timeout=120)
    create.raise_for_status()
    cid = create.json().get("id")
    if not cid:
        sys.exit(f"No reel container id returned: {create.text}")
    _wait_for_container(cid, token)
    pub = http("POST", f"{base}/media_publish",
               data={"creation_id": cid, "access_token": token}, timeout=120)
    pub.raise_for_status()
    return pub.json().get("id")


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


def _safe_key(s):
    return "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in str(s))


# =============================================================================
# Orchestration
# =============================================================================
def main():
    strategy = load_strategy()
    history = load_history()
    dry = os.environ.get("DRY_RUN") == "1"
    discover_only = os.environ.get("DISCOVER_ONLY") == "1"
    force = os.environ.get("FORCE") == "1"

    if force:
        slot, key = forced_slot(strategy)
        print("FORCE=1 — bypassing the schedule for this run.")
    else:
        slot, key = find_due_slot(strategy, history)

    if slot is None:
        local = local_now(strategy)
        print(f"[{BRAND_NAME}] {local.strftime('%a %Y-%m-%d %H:%M %Z')} — "
              "not a scheduled slot (or already posted / daily cap reached). "
              "Nothing to do.")
        return

    slot = dict(slot)
    if os.environ.get("SLOT_GAME"):
        slot["game"] = os.environ["SLOT_GAME"]
    if os.environ.get("SLOT_REGION"):
        slot["region"] = os.environ["SLOT_REGION"]
    if os.environ.get("SLOT_SOURCE"):
        slot["source"] = os.environ["SLOT_SOURCE"]
    print(f"[{BRAND_NAME}] slot {key} — game={slot['game']} "
          f"region={slot['region']} source={slot.get('source', 'twitch')}")

    clip = discover_clip(strategy, slot, history)
    if clip is None:
        print("No fresh qualifying clip for this slot — skipping (no post).")
        return
    print(f"Picked: {clip.get('view_count')} views | lang={clip.get('language')} | "
          f"{(clip.get('title') or '')[:60]}")
    print(f"  by {clip.get('broadcaster_name')} — {clip.get('url')}")

    ai = call_claude(clip, slot, strategy)
    caption, tags = assemble_caption(ai, clip, slot, strategy)
    burn = bool(strategy.get("burn_hook", False))
    hook = (ai.get("hook") or "").strip() if burn else ""
    print(f"Hook overlay: {hook!r}" + ("" if burn else " (burn_hook off — clean video, no text)"))
    print("Caption:\n" + caption)

    if discover_only:
        print("\nDISCOVER_ONLY=1 — stopping before download/build/publish.")
        return

    tmp = tempfile.gettempdir()
    raw = os.path.join(tmp, f"ck_raw_{_safe_key(clip['id'])}.mp4")
    reel = os.path.join(tmp, f"ck_reel_{_safe_key(clip['id'])}.mp4")
    print("Downloading clip...")
    download_clip(clip, raw)
    print("Reformatting to 9:16 Reel...")
    reformat_reel(raw, reel, hook, max_s=int(strategy.get("max_duration_s", 60)))
    print(f"Built reel: {reel} ({os.path.getsize(reel) // 1024} KB)")

    if dry:
        print("\nDRY_RUN=1 — built the reel; not publishing.")
        if r2_configured():
            pkey = f"previews/{_safe_key(clip['id'])}.mp4"
            purl = host_file_r2(reel, pkey, "video/mp4")
            print(f"Preview (hosted, NOT posted): {purl}")
        else:
            print("(R2 not configured — no preview URL; file is on the runner only.)")
        return

    if not r2_configured():
        sys.exit("Reels need R2 configured (a public video_url).")
    r2_key = f"reels/{_safe_key(clip['id'])}.mp4"
    video_url = host_file_r2(reel, r2_key, "video/mp4")
    print(f"Hosted: {video_url}")
    media_id = post_reel_to_instagram(video_url, caption)
    print(f"Published reel — media_id={media_id}")
    delete_from_r2([r2_key])

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
        "subreddit": clip.get("subreddit"),
        "author": clip.get("author"),
        "clip_id": clip["id"],
        "clip_url": clip.get("url"),
        "broadcaster": clip.get("broadcaster_name"),
        "creator": clip.get("creator_name"),
        "language": clip.get("language"),
        "clip_views": clip.get("view_count"),
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
