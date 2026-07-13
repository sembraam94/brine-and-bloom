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
import random
import datetime
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
    "hands, no people, no logos, no labels. Indistinguishable from a genuine, "
    "everyday food photo."
)

# AI disclosure appended to every caption. ⚠️ TEMPORARILY DISABLED (empty) to test
# performance without it. MUST be re-enabled before 2 Aug 2026, when the EU AI Act
# (Article 50) transparency obligations for AI-generated media take effect.
# Re-enable by setting the default below (or the AI_DISCLOSURE env/secret) back to
# e.g. "📷 AI food photography".
AI_DISCLOSURE = os.environ.get("AI_DISCLOSURE", "")

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

# Video (Reels): animate the Flux hero still into an ~8s appetizing clip. Veo's
# output is a public URL we hand straight to Instagram (like the Flux images).
VEO_MODEL = "google/veo-3.1-fast"   # image-to-video; caps at 8s/clip
REEL_DURATION = 8                   # seconds (Veo 3.1 Fast accepts 4, 6, or 8)
REEL_RESOLUTION = "1080p"           # 720p or 1080p
REEL_AUDIO = True                   # let Veo add ambient audio (Reels favor sound)

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

    n_images = "2 to 4" if fmt == "carousel" else "exactly 1"
    if fmt == "carousel":
        format_note = (
            " This is a CAROUSEL: describe a short visual SEQUENCE (e.g. raw "
            "ingredients laid out -> mid-process -> finished dish) as separate frames "
            "that tell one story and make people swipe and save."
        )
    elif fmt == "reel":
        format_note = (
            " This is a REEL (short ~8s video). image_prompts is ONE still (the hero "
            "start frame); ALSO return a 'video_prompt' describing the appetizing MOTION "
            "to animate it — subtle, realistic food motion with NO people and NO hands "
            "(e.g. a slow camera push-in over the dish, rising steam, a glistening glaze, "
            "a drizzle of sauce, a gentle sizzle). The recipe stays in the caption."
        )
    else:
        format_note = " This is a SINGLE image."

    video_prompt_spec = (
        ',\n  "video_prompt": "REEL only: a vivid description of the ~8s MOTION to animate '
        'the still — subtle, realistic food motion, NO people, NO hands"'
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
  "image_prompts": ["{n_images} vivid prompt(s) describing the SUBJECT and COMPOSITION only — the FINISHED, crave-worthy dish (the actual cooked/plated food) as the HERO in the center, styled to make people hungry, WITH the recipe's key raw ingredients named specifically (e.g. garlic cloves, a dish of honey-soy, fresh ginger, herbs, citrus) arranged around it to tell the story. Favor approachable, appetizing plating — bite-sized or sliced pieces that show the glaze and texture, rather than large whole cuts. Vary the plating, props, and angle from post to post so the feed has rhythm while keeping one consistent look. Do NOT describe lighting, camera, or art style; that is added automatically."]{video_prompt_spec}
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


def generate_image(subject_prompt):
    # SAMPLE_MODEL lets a sample run try a different model without changing the default.
    model = os.environ.get("SAMPLE_MODEL") or REPLICATE_MODEL
    full_prompt = f"{subject_prompt}\n\n{STYLE_SUFFIX}"
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
    if fmt != "carousel":
        image_prompts = image_prompts[:1]

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
        # One styled still -> animate it into an ~8s Reel. Recipe lives in caption.
        still_prompt = image_prompts[0]
        motion_prompt = post.get("video_prompt") or f"Slow, appetizing motion of the {theme} dish."
        print(f"  still prompt: {still_prompt}")
        print(f"  motion prompt: {motion_prompt}")
        still_url = generate_image(still_prompt)
        print(f"  still: {still_url}")
        print("Animating into a Reel (Veo)...")
        video_url = generate_video(still_url, motion_prompt)
        print(f"  video: {video_url}")

        if dry:
            print("\nDRY_RUN=1 — not publishing. Video URL above; caption below:")
            print("-" * 60)
            print(caption)
            print("-" * 60)
            return

        print("Publishing Reel to Instagram...")
        media_id = post_reel_to_instagram(video_url, caption)
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
