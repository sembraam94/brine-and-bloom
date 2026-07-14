#!/usr/bin/env python3
"""
Brine & Bloom — strategy-driven autonomous Instagram poster.

This is the POSTING half of a self-optimizing loop:

    strategy.json  ->  autopost.py (this file)  ->  history.json
         ^                                              |
         |                                              v
    analyze.py  <-----------  Instagram Insights  <-----+

`strategy.json` is the living playbook (owned and rewritten by analyze.py). It
says HOW OFTEN to post, AT WHAT TIMES, in WHAT FORMAT, and WHAT to post. This
script runs once an hour on GitHub Actions and asks a single question:

    "Is right now one of the scheduled slots, and have I not already posted it?"

If yes, it generates the post (Claude -> Flux) and publishes it (Graph API). If
no, it exits 0 immediately as a cheap no-op. So cadence and timing are 100%
controlled by strategy.json with no workflow/cron edits ever needed.

Env (set as GitHub Actions secrets — see README.md):
    ANTHROPIC_API_KEY, REPLICATE_API_TOKEN, IG_ACCESS_TOKEN
    (IG_USER_ID is optional — derived from the token if not set.)

Control env (optional):
    DRY_RUN=1   generate everything, print what it WOULD post, do not publish
    FORCE=1     ignore the schedule and post now (for the one-time setup test)
"""

import os
import sys
import json
import time
import math
import random
import shutil
import tempfile
import datetime
import subprocess
import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:                      # pragma: no cover (Python < 3.9)
    ZoneInfo = None

# =============================================================================
# CONFIG  —  the brand lives here. The *strategy* (timing/cadence/themes) lives
# in strategy.json and is rewritten automatically by analyze.py. Keep brand
# identity here; keep anything that should adapt to performance in strategy.json.
# =============================================================================

BRAND_NAME = "Brine & Bloom"

# Locked onto the END of every image prompt so the whole feed looks like one
# cohesive brand. Tweak once; every future post inherits it. NOTE: deliberately
# varied per-post composition is requested in call_claude() so the feed does not
# look templated/synthetic (an Instagram automation flag — see README safety).
STYLE_SUFFIX = (
    "A real, candid food photograph that looks like a person actually took it — "
    "NOT an AI render, NOT a glossy 3D image. The finished dish is the hero in the "
    "center, appetizing and fresh, with the recipe's key raw ingredients arranged "
    "naturally around it (a few garlic cloves, a small dish of marinade or oil, "
    "fresh herbs, citrus, whole spices) to tell the story. Real-food imperfections "
    "make it believable: a few stray crumbs, an uneven edge, a small sauce smudge "
    "on the plate, natural steam — nothing looks staged-perfect or plastic. "
    "Soft, slightly uneven natural window light with realistic soft shadows; a "
    "three-quarter or gently overhead angle; shallow depth of field. Textured "
    "natural surface — pale linen, weathered wood, or matte stoneware. Shot on a "
    "full-frame DSLR with a 50mm lens; fine natural film grain; true, honest "
    "colours (NOT over-saturated, NOT over-glossy, no CGI sheen). No text, no "
    "CRITICAL — the frame must be 100% TEXT-FREE: absolutely no text, letters, "
    "numbers, words, writing, captions, labels, signage, menus, or packaging text "
    "anywhere; every jar, bowl, bottle and surface is plain and unlabeled (any "
    "rendered text comes out as garbled gibberish, so there must be none). No "
    "hands, no people, no logos. Indistinguishable from a genuine, everyday food photo."
)

# AI disclosure appended to every caption — now ON as BRANDING (the account is
# openly an "AI chef"). Satisfies the EU AI Act (Article 50, from 2 Aug 2026) at
# zero reach cost. Framed as a feature, not a warning.
AI_DISCLOSURE = os.environ.get(
    "AI_DISCLOSURE", "🤖 Recipes dreamed up & plated by your AI chef"
)

STRATEGY_FILE = "strategy.json"
HISTORY_FILE = "history.json"
RECENT_TO_AVOID = 30            # how many recent titles Claude is told to avoid

# Hard limits the strategy can never exceed (Instagram rules + safety).
MAX_HASHTAGS = 5               # Instagram caps posts at 5 hashtags (Dec 2025).
MAX_CAPTION_CHARS = 2200       # Instagram caption hard limit.
MAX_CAROUSEL_IMAGES = 4        # keep carousels tight; Graph allows up to 10.
IMAGE_TIMEOUT_S = 300          # give up on a stuck Flux render after this many seconds
VIDEO_TIMEOUT_S = 600          # Veo renders take longer than images; cap the wait here

# Models / API versions (verified current as of 2026-06; see README).
CLAUDE_MODEL = "claude-sonnet-4-6"                  # the brain. claude-haiku-4-5 is cheaper.
REPLICATE_MODEL = "black-forest-labs/flux-1.1-pro-ultra"  # Ultra + raw = natural, less "AI" look

# Reels are now "AI chef" narrated slideshows: Flux stills + minimax TTS voiceover,
# assembled with ffmpeg (Ken Burns), hosted on R2, published as a Reel.
TTS_MODEL = "minimax/speech-2.8-hd"                       # #1-benchmark natural TTS
# Chosen voice. Alternatives to audition: Casual_Guy, Lively_Girl, Young_Knight. Empty env -> default.
CHEF_VOICE_ID = os.environ.get("CHEF_VOICE_ID") or "English_Trustworth_Man"
REEL_FPS = 30
REEL_MASCOT = os.environ.get("REEL_MASCOT", "assets/ai_chef.jpg")  # corner overlay; skipped if missing

# Legacy Veo image-to-video path (kept for the optional motion-clip style; unused
# by the default reel flow, which is the narrated slideshow above).
VEO_MODEL = "google/veo-3.1-fast"
REEL_DURATION = 8
REEL_RESOLUTION = "1080p"
REEL_AUDIO = True

# Instagram API with Instagram Login. Every call goes to graph.instagram.com and
# is authorized by IG_ACCESS_TOKEN (a long-lived Instagram User token — no Facebook
# Page involved). The account id is derived from the token, so IG_USER_ID is optional.
GRAPH_HOST = "https://graph.instagram.com"
GRAPH_VERSION = "v23.0"        # pinned; bump deliberately.


# =============================================================================
# Helpers
# =============================================================================

def env(name):
    """Read a required env var or exit loudly (a failed run emails you)."""
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Missing required environment variable: {name}")
    return value


_RETRY_STATUS = {429, 500, 502, 503, 504}


def http(method, url, *, retries=4, backoff=3, **kwargs):
    """requests with retry/backoff on transient failures (429 / 5xx / network
    blips). External APIs (Replicate, Anthropic, Instagram) all throw the
    occasional 502 etc.; for an unattended bot we want those to self-heal rather
    than fail a whole run. Returns the final Response (caller still calls
    raise_for_status); re-raises the last network error if all attempts fail."""
    last_exc = None
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
            last_exc = e
            if attempt < retries:
                wait = backoff * (2 ** attempt)
                print(f"  {e.__class__.__name__} on {url.split('?')[0]} — "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
                continue
            raise
    if last_exc:
        raise last_exc
    return resp


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def load_strategy():
    strat = _load_json(STRATEGY_FILE, None)
    if not strat or "slots" not in strat:
        sys.exit(
            f"{STRATEGY_FILE} is missing or has no slots. "
            "Commit a valid strategy.json (see README) or run "
            "`python analyze.py --strategize` to generate one."
        )
    return strat


def load_history():
    hist = _load_json(HISTORY_FILE, {"posts": []})
    if isinstance(hist, list):           # tolerate the old list-shaped history
        hist = {"posts": hist}
    hist.setdefault("posts", [])
    return hist


def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def local_now(strategy):
    tzname = strategy.get("timezone", "UTC")
    if ZoneInfo is None:
        return now_utc()
    try:
        return now_utc().astimezone(ZoneInfo(tzname))
    except Exception:
        return now_utc()


def slot_key(local_dt, slot):
    """Stable per-day-per-slot id so a late or double cron firing posts once."""
    return (
        f"{local_dt.year}-{local_dt.timetuple().tm_yday:03d}"
        f"-w{slot['weekday']}-{slot['hour']:02d}{slot.get('minute', 0):02d}"
    )


def already_posted(history, key):
    return any(p.get("slot_key") == key for p in history.get("posts", []))


def posts_today(history, local_dt):
    today = local_dt.date().isoformat()
    return sum(1 for p in history.get("posts", []) if p.get("local_date") == today)


def find_due_slot(strategy, history):
    """Return (slot, key) if the current moment is inside a scheduled slot's
    forward tolerance window and that slot hasn't been posted yet; else (None, None).

    Forward-only window (now >= slot time) absorbs GitHub cron drift (runs often
    fire 10-30 min late) without ever posting *early*.
    """
    local_dt = local_now(strategy)
    tol = strategy.get("tolerance_minutes", 90)
    max_per_day = strategy.get("cadence", {}).get("max_posts_per_day", 1)

    if posts_today(history, local_dt) >= max_per_day:
        return None, None

    for slot in strategy["slots"]:
        if local_dt.weekday() != slot["weekday"]:
            continue
        slot_dt = local_dt.replace(
            hour=slot["hour"], minute=slot.get("minute", 0),
            second=0, microsecond=0,
        )
        delta_min = (local_dt - slot_dt).total_seconds() / 60.0
        if 0 <= delta_min <= tol:
            key = slot_key(local_dt, slot)
            if not already_posted(history, key):
                return slot, key
    return None, None


def forced_slot(strategy):
    """For FORCE=1 setup tests: pick today's slot, else the first slot."""
    local_dt = local_now(strategy)
    for slot in strategy["slots"]:
        if slot["weekday"] == local_dt.weekday():
            return slot, f"forced-{local_dt.date()}-{local_dt.hour:02d}"
    slot = strategy["slots"][0]
    return slot, f"forced-{local_dt.date()}-{local_dt.hour:02d}"


# =============================================================================
# Step 1 — Claude decides the post (guided by the slot + accumulated learnings)
# =============================================================================

def call_claude(strategy, slot, recent_titles):
    theme = slot.get("theme", "general")
    fmt = slot.get("format", "image")
    content_plan = strategy.get("content_plan", {})
    theme_brief = content_plan.get(theme, slot.get("note", theme))
    learnings = strategy.get("learnings", "")
    caption_strategy = strategy.get(
        "caption_strategy",
        "Open with a keyword-rich one-line hook people would search for. "
        "Make it skimmable. Include an explicit 'save this' or 'send to a friend "
        "who cooks' beat — sends and saves are the top growth signals.",
    )
    hashtag_strategy = strategy.get(
        "hashtag_strategy",
        f"Return 3-{MAX_HASHTAGS} highly relevant hashtags, mostly mid-size niche "
        "tags, varied every single post (never a reused block).",
    )

    n_images = "3 to 4" if fmt in ("carousel", "reel") else "exactly 1"
    if fmt == "carousel":
        format_note = (
            " This is a CAROUSEL: describe a short visual SEQUENCE (e.g. raw "
            "ingredients laid out -> mid-process -> finished dish) as separate frames "
            "that tell one story and make people swipe and save."
        )
    elif fmt == "reel":
        format_note = (
            " This is a REEL narrated by a FUN, PERSONABLE, SELF-AWARE \"AI chef\" character "
            "talking to camera like a creator who KNOWS it's an AI and leans into it. "
            "image_prompts is 3-4 text-free food stills that tell the recipe visually (hero "
            "dish + a couple of ingredient/step shots). ALSO return a 'voiceover_script': "
            "~28-32 seconds of SPOKEN words (about 75-95 words). OPEN with a warm personal "
            "greeting introducing the chef + today's dish (vary it — e.g. 'Hey everyone, your "
            "AI chef here! Today we're making ...'). Walk through the recipe/ratio with "
            "personality, and weave in ONE small, witty SELF-AWARE meta-joke about being an "
            "AI (vary it every time — e.g. 'and yes, these are images, not video — rendering "
            "video is absurdly expensive and I'm trying to save some drinking water here', or "
            "a quip about being an AI that can't actually taste-test, or running hot on the "
            "GPUs). Keep the joke light and quick — one per reel, never forced, never at the "
            "recipe's expense. END with a save + 'follow your AI chef' CTA. Just the spoken "
            "words — no stage directions, no emojis, no scene labels."
        )
    else:
        format_note = " This is a SINGLE image."

    video_prompt_spec = (
        ',\n  "voiceover_script": "REEL only: ~75-95 words of SPOKEN narration for the fun, '
        'SELF-AWARE AI chef — warm greeting + dish intro, the recipe with personality, ONE '
        'small witty meta-joke about being an AI (e.g. using images not video to save '
        'compute/water, or cannot actually taste-test), then a save/follow CTA; spoken '
        'words only, no stage directions or emojis"'
        if fmt == "reel" else ""
    )

    system = f"""You are the creative director and head recipe writer for "{BRAND_NAME}",
an Instagram account about cooking tips, recipes, and marinades, paired with beautiful
stylized ingredient photography. Your job is to maximize FOLLOWER GROWTH on a young
account. The strongest 2026 growth signals, in order, are: sends/shares (someone DMs the
post to a friend), saves, then watch-time/likes. Engineer every post to be sent and saved.

Brand voice: warm, knowledgeable, confident — like a friend who's a great cook. Concise
and practical, never gushing. Minimal emoji. Recipes MUST be real, accurate, and food-safe.

CAPTION STRATEGY: {caption_strategy}
HASHTAG STRATEGY: {hashtag_strategy}

WHAT'S WORKING SO FAR (apply this — it is learned from real performance):
{learnings or "No performance data yet. Optimize for shareability, saves, and search-friendly captions."}

Write ONE post for today's slot. Format: {fmt}.{format_note}
Today's theme: {theme} — {theme_brief}

Return ONLY a JSON object with exactly these keys:
{{
  "title": "short internal label (used to avoid repeats); not shown publicly",
  "caption": "the full Instagram caption. Keyword-rich first line for search, then the tip/recipe in clean skimmable lines with simple measurements, then a clear save/share call-to-action. Do NOT put hashtags in here. Keep under 1800 characters.",
  "hashtags": ["3 to {MAX_HASHTAGS} hashtags, each starting with #. Mid-size niche tags preferred. Vary them from post to post — never reuse the same block."],
  "image_prompts": ["{n_images} vivid prompt(s) describing the SUBJECT and COMPOSITION only — the FINISHED, crave-worthy dish (the actual cooked/plated food) as the HERO in the center, styled to make people hungry, WITH the recipe's key raw ingredients named specifically (e.g. garlic cloves, a dish of honey-soy, fresh ginger, herbs, citrus) arranged around it to tell the story. Favor approachable, appetizing plating — bite-sized or sliced pieces that show the glaze and texture, rather than large whole cuts. Vary the plating, props, and angle from post to post so the feed has rhythm while keeping one consistent look. CRITICAL: NEVER describe or include any text, numbers, ratios, measurements, labels, signs, packaging, or writing in the image — the photo is pure text-free food photography and the model garbles any text. ALL recipe info (ratios, quantities, steps) goes ONLY in the caption, never on the image. Do NOT describe lighting, camera, or art style; that is added automatically."]{video_prompt_spec}
}}

Make today genuinely different from these recent posts (different recipe/technique/ingredient):
{json.dumps(recent_titles, ensure_ascii=False)}

Return the JSON and nothing else — no markdown fences, no commentary."""

    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1800,
        "system": system,
        "messages": [{
            "role": "user",
            "content": f"Generate today's {fmt} post for the '{theme}' slot now.",
        }],
    }

    resp = http(
        "POST",
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": env("ANTHROPIC_API_KEY"),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=body,
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()

    text = "".join(
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ).strip()

    if text.startswith("```"):                       # tolerate code fences
        text = text.split("```")[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
        text = text.strip()

    return json.loads(text)


# =============================================================================
# Step 2 — render the image(s) (Flux via Replicate; returns public URLs)
# =============================================================================

def _image_input(model, full_prompt):
    """Model-appropriate input dict (Flux and Imagen take different params)."""
    if "imagen" in model:
        return {"prompt": full_prompt, "aspect_ratio": "1:1",
                "output_format": "jpg", "safety_filter_level": "block_only_high"}
    inp = {"prompt": full_prompt, "aspect_ratio": "1:1",   # "4:5" = taller
           "output_format": "jpg", "safety_tolerance": 2}
    if "ultra" in model:
        inp["raw"] = True              # Flux Ultra raw mode: natural, less synthetic
    return inp


def generate_image(subject_prompt, use_style=True):
    # SAMPLE_MODEL lets a sample run try a different model without changing the default.
    # use_style=False generates a raw prompt (e.g. the mascot) without the food STYLE_SUFFIX.
    model = os.environ.get("SAMPLE_MODEL") or REPLICATE_MODEL
    full_prompt = f"{subject_prompt}\n\n{STYLE_SUFFIX}" if use_style else subject_prompt
    token = env("REPLICATE_API_TOKEN")

    resp = http(
        "POST",
        f"https://api.replicate.com/v1/models/{model}/predictions",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Prefer": "wait",
        },
        json={"input": _image_input(model, full_prompt)},
        timeout=180,
    )
    resp.raise_for_status()
    prediction = resp.json()

    # Hard cap so a render stuck in Replicate's queue can't hang the whole job
    # for hours — fail fast and let the next scheduled run retry instead.
    deadline = time.monotonic() + IMAGE_TIMEOUT_S
    while prediction.get("status") not in ("succeeded", "failed", "canceled"):
        if time.monotonic() > deadline:
            sys.exit(f"Image generation timed out after {IMAGE_TIMEOUT_S}s "
                     f"(Replicate slow/stuck) — skipping this run; will retry next slot.")
        time.sleep(3)
        poll = http(
            "GET",
            prediction["urls"]["get"],
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
        poll.raise_for_status()
        prediction = poll.json()

    if prediction.get("status") != "succeeded":
        sys.exit(f"Image generation failed: {prediction.get('error')}")

    output = prediction["output"]
    return output[0] if isinstance(output, list) else output


def generate_images(prompts):
    return [generate_image(p) for p in prompts]


def generate_video(image_url, motion_prompt):
    """Animate the styled hero still into an ~8s Reel via Veo (image-to-video).
    The look is inherited from the start image; the prompt drives the MOTION.
    Returns a public video URL (Replicate delivery) we hand straight to Instagram."""
    token = env("REPLICATE_API_TOKEN")
    full_prompt = (
        f"{motion_prompt} Subtle, realistic motion; warm natural light; "
        "appetizing and mouth-watering; no people, no hands; photorealistic, not a render."
    )

    resp = http(
        "POST",
        f"https://api.replicate.com/v1/models/{VEO_MODEL}/predictions",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"input": {
            "prompt": full_prompt,
            "image": image_url,            # start frame (image-to-video)
            "aspect_ratio": "9:16",        # vertical, for Reels
            "resolution": REEL_RESOLUTION,
            "duration": REEL_DURATION,
            "generate_audio": REEL_AUDIO,
        }},
        timeout=180,
    )
    resp.raise_for_status()
    prediction = resp.json()

    deadline = time.monotonic() + VIDEO_TIMEOUT_S
    while prediction.get("status") not in ("succeeded", "failed", "canceled"):
        if time.monotonic() > deadline:
            sys.exit(f"Video generation timed out after {VIDEO_TIMEOUT_S}s "
                     f"(Veo slow/stuck) — skipping this run; will retry next slot.")
        time.sleep(5)
        poll = http(
            "GET",
            prediction["urls"]["get"],
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
        poll.raise_for_status()
        prediction = poll.json()

    if prediction.get("status") != "succeeded":
        sys.exit(f"Video generation failed: {prediction.get('error')}")

    output = prediction["output"]
    return output[0] if isinstance(output, list) else output


# =============================================================================
# Step 2b — strip AI-provenance metadata + re-host on R2 (optional)
#
# If R2 is configured, each generated image is downloaded, RE-ENCODED (which drops
# C2PA / IPTC "digital source type" / XMP / EXIF provenance tags), uploaded to R2,
# and Instagram is given that clean URL instead of the raw generator URL. If R2 is
# NOT configured, images are posted straight from the generator URL (unchanged).
# NOTE: this removes metadata-based AI signals only — Meta's pixel classifier can
# still detect AI imagery. See README.
# =============================================================================

R2_ENV = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
          "R2_BUCKET", "R2_PUBLIC_BASE_URL")


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


def strip_and_host(image_url, key):
    """Download -> re-encode as a clean JPEG (drops provenance metadata) -> upload
    to R2 -> return the public URL Instagram will fetch."""
    import io
    from PIL import Image
    r = http("GET", image_url, timeout=120)
    r.raise_for_status()
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)   # no exif/metadata carried over
    _r2_client().put_object(
        Bucket=env("R2_BUCKET"), Key=key,
        Body=buf.getvalue(), ContentType="image/jpeg",
    )
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


def prepare_image_urls(replicate_urls, slot_key):
    """Return (public_urls, r2_keys). With R2 configured: strip metadata + re-host
    each image (keys are deleted after publishing). Without R2: pass through."""
    if not r2_configured():
        return replicate_urls, []
    print("Stripping AI-provenance metadata + re-hosting on R2...")
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in str(slot_key))
    urls, keys = [], []
    for i, u in enumerate(replicate_urls):
        k = f"img/{safe}-{i}.jpg"
        urls.append(strip_and_host(u, k))
        keys.append(k)
        print(f"  hosted: {urls[-1]}")
    return urls, keys


# =============================================================================
# Step 2c — AI chef reel: minimax TTS narration + ffmpeg slideshow
# =============================================================================

def _safe_key(s):
    return "".join(c if (c.isalnum() or c in "-_") else "-" for c in str(s))


def _download(url, path):
    r = http("GET", url, timeout=180)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)
    return path


def host_file_r2(local_path, key, content_type):
    """Upload any local file to R2 and return its public URL."""
    with open(local_path, "rb") as f:
        _r2_client().put_object(Bucket=env("R2_BUCKET"), Key=key,
                                Body=f.read(), ContentType=content_type)
    return f"{env('R2_PUBLIC_BASE_URL').rstrip('/')}/{key}"


def generate_narration(script, out_path):
    """minimax TTS -> download the mp3 to out_path. Returns out_path."""
    token = env("REPLICATE_API_TOKEN")
    resp = http(
        "POST",
        f"https://api.replicate.com/v1/models/{TTS_MODEL}/predictions",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json", "Prefer": "wait"},
        json={"input": {
            "text": script, "voice_id": CHEF_VOICE_ID, "emotion": "happy",
            "speed": 1.05, "volume": 1.4, "audio_format": "mp3",
            "sample_rate": 44100, "bitrate": 256000, "channel": "mono",
            "english_normalization": True, "language_boost": "English",
        }},
        timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()
    while data.get("status") not in ("succeeded", "failed", "canceled"):
        time.sleep(3)
        p = http("GET", data["urls"]["get"],
                 headers={"Authorization": f"Bearer {token}"}, timeout=60)
        p.raise_for_status()
        data = p.json()
    if data.get("status") != "succeeded":
        sys.exit(f"Narration (TTS) failed: {data.get('error')}")
    out = data.get("output")
    audio_url = (out if isinstance(out, str)
                 else out[0] if isinstance(out, list) and out
                 else out.get("audio") if isinstance(out, dict) else None)
    if not audio_url:
        sys.exit(f"No audio URL in TTS output: {out}")
    return _download(audio_url, out_path)


def _ensure_ffmpeg():
    if shutil.which("ffmpeg"):
        return
    print("Installing ffmpeg...")
    subprocess.run(["sudo", "apt-get", "update", "-qq"], check=True)
    subprocess.run(["sudo", "apt-get", "install", "-y", "-qq", "ffmpeg"], check=True)


def _srt_ts(s):
    ms = int(round(s * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def write_srt(script, duration, path):
    """Split the narration into short phrases timed proportionally across the
    audio (so captions roughly track the speech). Returns path or None."""
    import re
    chunks = []
    for sent in re.split(r"(?<=[.!?])\s+", script.strip()):
        words = sent.split()
        for i in range(0, len(words), 6):
            c = " ".join(words[i:i + 6]).strip()
            if c:
                chunks.append(c)
    if not chunks:
        return None
    total = sum(len(c) for c in chunks) or 1
    t, out = 0.0, []
    for i, c in enumerate(chunks, 1):
        seg = duration * (len(c) / total)
        out.append(f"{i}\n{_srt_ts(t)} --> {_srt_ts(t + seg)}\n{c}\n")
        t += seg
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    return path


def build_reel(image_paths, audio_path, out_path, caption_text=None, mascot_path=None):
    """1080x1920 H.264/AAC Reel: N stills with Ken Burns motion, length matched to
    the narration, optional burned-in captions and an AI-chef mascot overlay."""
    _ensure_ffmpeg()
    tmp = tempfile.gettempdir()
    dur = float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", audio_path]).strip())
    n = len(image_paths)
    dframes = int(math.ceil(dur / n * REEL_FPS)) + 1
    W, H, SUP = 1080, 1920, 2
    BW, BH = W * SUP, H * SUP

    inputs = []
    for p in image_paths:
        inputs += ["-i", p]
    inputs += ["-i", audio_path]
    mascot_idx = None
    if mascot_path and os.path.exists(mascot_path):
        inputs += ["-i", mascot_path]
        mascot_idx = n + 1

    fc, cc = "", ""
    for i in range(n):
        z = ("z='min(zoom+0.0009,1.20)'" if i % 2 == 0
             else "z='if(eq(on,0),1.20,max(1.0,zoom-0.0009))'")
        fc += (f"[{i}:v]scale={BW}:{BH}:force_original_aspect_ratio=increase:flags=lanczos,"
               f"crop={BW}:{BH},setsar=1,"
               f"zoompan={z}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
               f"d={dframes}:s={W}x{H}:fps={REEL_FPS},format=yuv420p,setsar=1[v{i}];")
        cc += f"[v{i}]"
    fc += f"{cc}concat=n={n}:v=1:a=0[vc];"
    cur = "vc"

    if caption_text:
        srt = write_srt(caption_text, dur, os.path.join(tmp, "bb_subs.srt"))
        if srt:
            style = ("Alignment=2,FontName=DejaVu Sans,Fontsize=15,Bold=1,"
                     "PrimaryColour=&H00FFFFFF,BorderStyle=3,BackColour=&H99000000,"
                     "Outline=1,Shadow=0,MarginV=170")
            fc += f"[{cur}]subtitles=filename='{srt}':force_style='{style}'[vs];"
            cur = "vs"

    if mascot_idx is not None:
        fc += (f"[{mascot_idx}:v]scale=300:-1[mask];"
               f"[{cur}][mask]overlay=x=W-w-40:y=64[vm];")
        cur = "vm"

    fc = fc.rstrip(";")
    subprocess.run([
        "ffmpeg", "-y", *inputs, "-filter_complex", fc,
        "-map", f"[{cur}]", "-map", f"{n}:a",
        "-r", str(REEL_FPS), "-c:v", "libx264", "-profile:v", "high",
        "-pix_fmt", "yuv420p", "-crf", "20", "-maxrate", "8M", "-bufsize", "12M",
        "-g", "60", "-x264-params", "keyint=60:min-keyint=60:scenecut=0",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", "-shortest", out_path,
    ], check=True)
    return out_path


def compose_badge(char_path, out_path):
    """Circular badge with the character standing IN FRONT of it: the disc is a
    background, the whole character (hat, head, torso — nothing clipped by the
    circle) sits on top, trimmed only at the bottom so it looks like he's popping
    out of the badge. Returns out_path (transparent PNG)."""
    from PIL import Image, ImageDraw
    char = Image.open(char_path).convert("RGBA")
    bb = char.getbbox()
    if bb:
        char = char.crop(bb)
    cw, ch = char.size
    W = 620
    char = char.resize((W, max(1, int(ch * W / cw))))
    cw, ch = char.size
    D = int(cw * 1.02)              # disc roughly the character's width
    ring = 16
    circle_top = int(ch * 0.30)     # head + hat sit above the disc's top edge
    cx = cw // 2
    cy = circle_top + D // 2
    clip_y = cy + int(D * 0.14)      # trim the character here; disc's lower arc shows below
    Hh = max(ch, circle_top + D) + ring + 8
    canvas = Image.new("RGBA", (cw, Hh), (0, 0, 0, 0))
    dr = ImageDraw.Draw(canvas)
    # 1) disc + ring in the BACKGROUND
    dr.ellipse([cx - D // 2 - ring, cy - D // 2 - ring,
                cx + D // 2 + ring, cy + D // 2 + ring], fill=(244, 235, 221, 255))  # cream ring
    dr.ellipse([cx - D // 2, cy - D // 2, cx + D // 2, cy + D // 2],
               fill=(95, 107, 65, 255))                                             # olive disc
    # 2) whole character ON TOP, trimmed only below clip_y (not clipped to the circle)
    mask = Image.new("L", (cw, Hh), 0)
    ImageDraw.Draw(mask).rectangle([0, 0, cw, clip_y], fill=255)
    layer = Image.new("RGBA", (cw, Hh), (0, 0, 0, 0))
    layer.alpha_composite(char, (0, 0))
    r, g, b, a = layer.split()
    a = Image.composite(a, Image.new("L", (cw, Hh), 0), mask)
    canvas.alpha_composite(Image.merge("RGBA", (r, g, b, a)))
    canvas.save(out_path)
    return out_path


BG_REMOVAL_MODEL = "men1scus/birefnet"   # SOTA transparent-cutout; community model


def _replicate_predict(model, inp, token):
    """Run a community Replicate model via the versioned /v1/predictions endpoint
    (the /models/{model}/predictions shortcut only works for official models)."""
    mr = http("GET", f"https://api.replicate.com/v1/models/{model}",
              headers={"Authorization": f"Bearer {token}"}, timeout=60)
    mr.raise_for_status()
    version = (mr.json().get("latest_version") or {}).get("id")
    if not version:
        sys.exit(f"No version found for {model}")
    resp = http(
        "POST", "https://api.replicate.com/v1/predictions",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json", "Prefer": "wait"},
        json={"version": version, "input": inp}, timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()
    while data.get("status") not in ("succeeded", "failed", "canceled"):
        time.sleep(3)
        pr = http("GET", data["urls"]["get"],
                  headers={"Authorization": f"Bearer {token}"}, timeout=60)
        pr.raise_for_status()
        data = pr.json()
    if data.get("status") != "succeeded":
        sys.exit(f"{model} failed: {data.get('error')}")
    return data.get("output")


def build_badge(src_path="assets/ai_chef.jpg"):
    """Remove the mascot background, build the 'head popping out of a circle'
    badge, and host a preview on R2."""
    src_url = host_file_r2(src_path, "tmp-mascot-src.jpg", "image/jpeg")
    token = env("REPLICATE_API_TOKEN")
    out = _replicate_predict(BG_REMOVAL_MODEL, {"image": src_url}, token)
    png_url = out if isinstance(out, str) else (out[0] if isinstance(out, list) and out else None)
    if not png_url:
        sys.exit(f"No bg-removal output: {out}")
    tmp = tempfile.gettempdir()
    char = _download(png_url, os.path.join(tmp, "bb_char.png"))
    badge = compose_badge(char, os.path.join(tmp, "bb_badge.png"))
    url = host_file_r2(badge, "mascot-badge-preview.png", "image/png")
    print(f"badge preview: {url}")
    return url


# =============================================================================
# Step 3 — publish to Instagram (single image or carousel)
# =============================================================================

def _graph_node(node):
    return f"{GRAPH_HOST}/{GRAPH_VERSION}/{node}"


def _graph_base(ig_id):
    return _graph_node(ig_id)


_IG_USER_ID_CACHE = None


def resolve_ig_user_id(token):
    """The Instagram account id used in every publish/insights call. Uses the
    IG_USER_ID env override if set; otherwise derives it from the token via
    /me?fields=user_id. NOTE: it must be the `user_id` field, not the app-scoped
    `id` field — using `id` is the classic bug in this flow."""
    global _IG_USER_ID_CACHE
    if os.environ.get("IG_USER_ID"):
        return os.environ["IG_USER_ID"]
    if _IG_USER_ID_CACHE:
        return _IG_USER_ID_CACHE
    r = http(
        "GET",
        _graph_node("me"),
        params={"fields": "user_id,username", "access_token": token},
        timeout=60,
    )
    r.raise_for_status()
    uid = r.json().get("user_id")
    if not uid:
        sys.exit(f"Could not resolve IG user_id from token: {r.text}")
    _IG_USER_ID_CACHE = str(uid)
    return _IG_USER_ID_CACHE


def last_media(ig_id, token):
    """Most recent published media on the account: {'id', 'time'(UTC)} or None.

    This is our independent source of truth for the 'already posted this slot'
    check — it does NOT depend on history.json having been committed, so it
    survives the publish-succeeded-but-git-push-failed case (a real risk that
    would otherwise double-post within the tolerance window)."""
    try:
        r = http(
            "GET",
            f"{_graph_base(ig_id)}/media",
            params={"fields": "timestamp", "limit": 1, "access_token": token},
            timeout=60,
        )
        r.raise_for_status()
        items = r.json().get("data", [])
        if not items:
            return None
        item = items[0]
        ts = item.get("timestamp")
        when = None
        if ts:
            try:
                when = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                when = None
        return {"id": item.get("id"), "time": when}
    except Exception as e:
        # Never let a guard lookup failure block posting; just skip the guard.
        print(f"(Could not read recent media for the repost guard: {e})")
        return None


def slot_already_live(slot, local_dt, ig_id, token):
    """True if the account already has a post published at/after this slot's
    start today — meaning a previous run published it but may have failed to
    commit history.json. Returns the live media id too, for recovery logging."""
    last = last_media(ig_id, token)
    if not last or not last.get("time"):
        return False, None
    slot_start_local = local_dt.replace(
        hour=slot["hour"], minute=slot.get("minute", 0), second=0, microsecond=0,
    )
    slot_start_utc = slot_start_local.astimezone(datetime.timezone.utc)
    if last["time"] >= slot_start_utc:
        return True, last.get("id")
    return False, None


def check_publishing_quota(ig_id, token):
    """Stop early if we've hit Instagram's rolling 24h publish cap (read live;
    currently 100, but the value is account/version dependent so we never hardcode)."""
    try:
        r = http(
            "GET",
            f"{_graph_base(ig_id)}/content_publishing_limit",
            params={"fields": "config,quota_usage", "access_token": token},
            timeout=60,
        )
        r.raise_for_status()
        d = r.json().get("data", [{}])[0]
        used = d.get("quota_usage", 0)
        total = d.get("config", {}).get("quota_total", 100)
        if used >= total:
            sys.exit(f"Publish quota reached ({used}/{total} in last 24h). Skipping.")
        print(f"Publish quota: {used}/{total} used in last 24h.")
    except requests.HTTPError as e:
        print(f"(Could not read publishing quota, continuing: {e})")


def _wait_for_container(container_id, token, max_wait=120, interval=3):
    """Poll a media container until FINISHED. Images finish almost instantly;
    Reels need transcoding, so the reel path passes a longer max_wait/interval."""
    status_url = _graph_node(container_id)
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        r = http(
            "GET",
            status_url,
            params={"fields": "status_code", "access_token": token},
            timeout=60,
        )
        r.raise_for_status()
        status = r.json()
        code = status.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            sys.exit(f"Instagram container {code}: {status}")
        time.sleep(interval)
    sys.exit("Instagram container never reached FINISHED (timed out).")


def post_to_instagram(image_urls, caption):
    token = env("IG_ACCESS_TOKEN")
    ig_id = resolve_ig_user_id(token)
    base = _graph_base(ig_id)

    check_publishing_quota(ig_id, token)

    if len(image_urls) == 1:
        create = http(
            "POST",
            f"{base}/media",
            data={"image_url": image_urls[0], "caption": caption,
                  "access_token": token},
            timeout=120,
        )
        create.raise_for_status()
        container_id = create.json().get("id")
        if not container_id:
            sys.exit(f"No media container id returned: {create.text}")
        _wait_for_container(container_id, token)
    else:
        # Carousel: a child container per image, then a parent that holds them.
        child_ids = []
        for url in image_urls[:MAX_CAROUSEL_IMAGES]:
            child = http(
                "POST",
                f"{base}/media",
                data={"image_url": url, "is_carousel_item": "true",
                      "access_token": token},
                timeout=120,
            )
            child.raise_for_status()
            cid = child.json().get("id")
            if not cid:
                sys.exit(f"No carousel child id returned: {child.text}")
            _wait_for_container(cid, token)
            child_ids.append(cid)

        parent = http(
            "POST",
            f"{base}/media",
            data={"media_type": "CAROUSEL", "children": ",".join(child_ids),
                  "caption": caption, "access_token": token},
            timeout=120,
        )
        parent.raise_for_status()
        container_id = parent.json().get("id")
        if not container_id:
            sys.exit(f"No carousel parent id returned: {parent.text}")
        _wait_for_container(container_id, token)

    publish = http(
        "POST",
        f"{base}/media_publish",
        data={"creation_id": container_id, "access_token": token},
        timeout=120,
    )
    publish.raise_for_status()
    return publish.json().get("id")


def post_reel_to_instagram(video_url, caption):
    """Publish a Reel: REELS container with a public video_url -> wait for
    transcoding (longer than images) -> publish."""
    token = env("IG_ACCESS_TOKEN")
    ig_id = resolve_ig_user_id(token)
    base = _graph_base(ig_id)

    check_publishing_quota(ig_id, token)

    create = http(
        "POST",
        f"{base}/media",
        data={"media_type": "REELS", "video_url": video_url, "caption": caption,
              "share_to_feed": "true", "access_token": token},
        timeout=120,
    )
    create.raise_for_status()
    container_id = create.json().get("id")
    if not container_id:
        sys.exit(f"No reel container id returned: {create.text}")

    # Reels transcode server-side; give it room (poll ~every 10s up to ~7 min).
    _wait_for_container(container_id, token, max_wait=420, interval=10)

    publish = http(
        "POST",
        f"{base}/media_publish",
        data={"creation_id": container_id, "access_token": token},
        timeout=120,
    )
    publish.raise_for_status()
    return publish.json().get("id")


# =============================================================================
# Orchestration
# =============================================================================

def build_caption(post):
    body = post.get("caption", "").strip()
    hashtags = [h for h in post.get("hashtags", []) if isinstance(h, str)][:MAX_HASHTAGS]
    parts = [body]
    if AI_DISCLOSURE:
        parts.append(AI_DISCLOSURE)
    if hashtags:
        parts.append(" ".join(hashtags))
    return "\n\n".join(parts).strip()[:MAX_CAPTION_CHARS], hashtags


def main():
    # RAW_PROMPT: one-off asset generation (e.g. the AI-chef mascot) — no Claude,
    # no food style, just the given prompt -> hosted on R2. For SAMPLE/asset use.
    raw = os.environ.get("RAW_PROMPT")
    if raw:
        if not r2_configured():
            sys.exit("RAW_PROMPT asset generation needs R2 configured.")
        print(f"RAW asset gen: {raw[:70]}")
        url = generate_image(raw, use_style=False)
        tmp = tempfile.gettempdir()
        path = _download(url, os.path.join(tmp, "bb_asset.jpg"))
        hosted = host_file_r2(path, "asset-" + _safe_key(raw[:24]) + ".jpg", "image/jpeg")
        print(f"asset hosted: {hosted}")
        return

    if os.environ.get("BUILD_BADGE") == "1":
        if not r2_configured():
            sys.exit("BUILD_BADGE needs R2 configured.")
        build_badge()
        return

    strategy = load_strategy()
    history = load_history()
    dry = os.environ.get("DRY_RUN") == "1"
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

    fmt = slot.get("format", "image")
    fmt_override = os.environ.get("FORMAT")
    if fmt_override in ("image", "carousel", "reel"):
        fmt = fmt_override
        print(f"FORMAT override: posting as {fmt}")
    slot = {**slot, "format": fmt}   # keep call_claude + the record consistent with the effective format
    theme = slot.get("theme", "general")
    recent_titles = [p.get("title", "") for p in history["posts"][-RECENT_TO_AVOID:]]

    print(f"[{BRAND_NAME}] slot={key} format={fmt} theme={theme}")

    # Repost guard (independent of git state): if a previous run already published
    # this slot but failed to commit history.json, the live account is the truth.
    # Record it and skip rather than double-posting. (Skipped for DRY/FORCE.)
    if not dry and not force:
        ig_token = env("IG_ACCESS_TOKEN")
        ig_id = resolve_ig_user_id(ig_token)
        live, live_id = slot_already_live(slot, local_now(strategy), ig_id, ig_token)
        if live:
            print(f"Slot {key} already published on the account "
                  f"(media {live_id}); recovering history and skipping.")
            history["posts"].append({
                "slot_key": key,
                "date_utc": now_utc().isoformat(),
                "local_date": local_now(strategy).date().isoformat(),
                "weekday": slot["weekday"], "slot_hour": slot["hour"],
                "format": fmt, "theme": theme, "title": "(recovered)",
                "hashtags": [], "media_id": live_id,
                "metrics": {}, "measured_at": None, "recovered": True,
            })
            save_history(history)
            return

    post = call_claude(strategy, slot, recent_titles)
    title = post.get("title", "(untitled)")
    image_prompts = [p for p in post.get("image_prompts", []) if isinstance(p, str)]
    if not image_prompts:
        sys.exit("Claude returned no image_prompts.")
    if fmt == "image":
        image_prompts = image_prompts[:1]   # carousel + reel keep multiple stills

    caption, hashtags = build_caption(post)
    print(f"Title: {title}")
    print(f"Caption preview: {caption[:140]}...")
    print(f"Hashtags ({len(hashtags)}): {' '.join(hashtags)}")

    # Jitter: never post at a perfectly fixed clock minute (a bot tell). Cron
    # drift already varies timing; this adds a little more. Only on real runs.
    jitter = strategy.get("jitter_minutes", 12)
    if not dry and not force and jitter > 0:
        wait_s = random.randint(0, jitter * 60)
        print(f"Jitter: sleeping {wait_s}s before publishing.")
        time.sleep(wait_s)

    if fmt == "reel":
        # AI-chef narrated Reel: 3-4 stills + minimax TTS voiceover, assembled with
        # ffmpeg (Ken Burns slideshow + burned-in captions + optional mascot),
        # hosted on R2, published as a Reel.
        if not r2_configured():
            sys.exit("Reels need R2 configured (the video host) — set the R2 secrets.")
        script = (post.get("voiceover_script") or "").strip()
        if not script:
            sys.exit("Claude returned no voiceover_script for the reel.")
        still_prompts = image_prompts[:4]
        print(f"  script ({len(script.split())} words): {script}")
        for i, p in enumerate(still_prompts, 1):
            print(f"  still {i}: {p}")
        tmp = tempfile.gettempdir()
        still_paths = []
        for i, p in enumerate(still_prompts):
            u = generate_image(p)
            still_paths.append(_download(u, os.path.join(tmp, f"bb_still{i}.jpg")))
        print("Generating AI-chef narration (minimax TTS)...")
        audio_path = generate_narration(script, os.path.join(tmp, "bb_narration.mp3"))
        print("Assembling the Reel with ffmpeg (Ken Burns + captions)...")
        mascot = REEL_MASCOT if os.path.exists(REEL_MASCOT) else None
        mp4 = build_reel(still_paths, audio_path, os.path.join(tmp, "bb_reel.mp4"),
                         caption_text=script, mascot_path=mascot)

        persist = dry or os.environ.get("SAMPLE") == "1"
        vsuffix = "-" + _safe_key(CHEF_VOICE_ID) if persist else ""   # distinct sample per voice
        vkey = ("sample-reel-" if persist else "reel-") + _safe_key(key) + vsuffix + ".mp4"
        video_url = host_file_r2(mp4, vkey, "video/mp4")
        print(f"  reel video: {video_url}")
        if persist:
            print("\n(dry/sample) — Reel hosted on R2 above, not posted.")
            return
        print("Publishing Reel to Instagram...")
        media_id = post_reel_to_instagram(video_url, caption)
        delete_from_r2([vkey])
    else:
        print(f"Generating {len(image_prompts)} image(s)...")
        for i, p in enumerate(image_prompts, 1):
            print(f"  prompt {i}: {p}")
        image_urls = generate_images(image_prompts)
        for u in image_urls:
            print(f"  image: {u}")

        if os.environ.get("SAMPLE") == "1":
            # Host the preview on R2 (persistent URL) instead of the expiring
            # generator URL, and do NOT post or record anything.
            prepare_image_urls(image_urls, "sample-" + key)
            print("\nSAMPLE — hosted on R2 above (persistent, not posted).")
            return

        if dry:
            print("\nDRY_RUN=1 — not publishing. This is what it WOULD post:")
            print("-" * 60)
            print(caption)
            print("-" * 60)
            return

        image_urls, r2_keys = prepare_image_urls(image_urls, key)
        print("Publishing to Instagram...")
        media_id = post_to_instagram(image_urls, caption)
        delete_from_r2(r2_keys)

    print(f"Published. Media ID: {media_id}")

    local = local_now(strategy)
    history["posts"].append({
        "slot_key": key,
        "date_utc": now_utc().isoformat(),
        "local_date": local.date().isoformat(),
        "weekday": slot["weekday"],
        "slot_hour": slot["hour"],
        "format": fmt,
        "theme": theme,
        "title": title,
        "hashtags": hashtags,
        "media_id": media_id,
        "metrics": {},          # filled in later by analyze.py
        "measured_at": None,
    })
    save_history(history)
    print("History updated.")


if __name__ == "__main__":
    main()
