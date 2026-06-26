# CLAUDE.md — Brine & Bloom

Context for Claude Code working in this repo. Read this fully before acting.

## What this project is

A **self-optimizing autonomous Instagram account** for cooking tips, recipes, and
marinades with AI-styled ingredient photography. After a one-time human setup it
runs with no person in the loop and **learns from performance**: it posts on a
strategy-defined schedule, measures how each post did, and rewrites its own
strategy to grow followers faster. The scheduler is **GitHub Actions** (free, no
server).

## The loop

```
strategy.json  ->  autopost.py  ->  Instagram  ->  Insights  ->  analyze.py  -.
 (playbook)        (posts when                       (measure)   (rewrites     |
     ^              a slot is due)                                 strategy)    |
     '-------------------------------------------------------------------------'
```

## Your role

You build, verify, test, debug, and extend this project, and you guide the human
through the steps only they can do. You write and refine code, scaffold the repo,
and run **dry-run** tests (`DRY_RUN=1`). You do **not** create accounts, handle
raw credentials, or publish a live post without explicit human confirmation (see
*Never do*).

## Architecture / data flow

Two scheduled GitHub Actions jobs:

**`autopost.py` — hourly poster.** Each hour: load `strategy.json` + `history.json`;
compute "now" in the strategy's timezone; if it falls inside a slot's forward
tolerance window AND that slot isn't already in history (slot_key dedupe) AND the
daily cap isn't hit → generate and publish, else exit 0. When posting: Claude
(`claude-sonnet-4-6`) writes title/caption/hashtags/image_prompts → Flux
(`black-forest-labs/flux-1.1-pro`) renders 1 image (or 2–4 for a carousel) →
publish via the **Instagram API with Instagram Login** (`graph.instagram.com`,
`v23.0`: create container → poll status_code=FINISHED → media_publish; carousels
use child + parent containers) → append the post (with media_id) to `history.json`.
The account id is derived from the token (`resolve_ig_user_id()` → /me?fields=user_id);
no Facebook Page is involved.

**`analyze.py` — daily analyzer/strategist.** Reads the live follower count
(`followers_count` field — works below 100 followers) into `followers.json`;
pulls per-post Insights (`reach, likes, comments, saved, shares,
total_interactions, views, profile_visits, follows`) into `history.json`; and
weekly (or `--strategize`) hands the numbers to Claude to rewrite `strategy.json`,
**clamped** to a hard safety envelope in `clamp_strategy()`.

Each job commits its state files back; those commits also keep the schedule from
being auto-disabled after 60 days.

## Repository layout

```
autopost.py                     # poster (brand config at top)
analyze.py                      # analyzer + strategist (imports config from autopost)
strategy.json                   # THE PLAYBOOK: timezone, cadence, slots, content_plan, learnings
history.json                    # auto-created: post records + per-post metrics — do NOT hand-edit
followers.json                  # auto-created: [{date, followers, media_count}]
refresh_token.py                # refreshes the IG token (ig_refresh_token; no app secret)
requirements.txt                # requests, tzdata
.github/workflows/post.yml          # hourly cron, gating, commit-back
.github/workflows/analyze.yml       # daily cron, metrics + strategy, commit-back
.github/workflows/refresh-token.yml # weekly token refresh (needs GH_PAT to store it)
.gitignore                      # ignores .env, __pycache__/, venv/
README.md                       # human setup walkthrough
CLAUDE.md                       # this file
```

If a source file is missing, recreate it to match this and README.md.

## Shared data contract (keep autopost.py and analyze.py in sync)

A `history.json` post record has exactly these fields — both files depend on them:
`slot_key`, `date_utc` (ISO, tz-aware), `local_date` (YYYY-MM-DD in strategy tz),
`weekday` (0=Mon), `slot_hour`, `format` (`image`|`carousel`), `theme`, `title`,
`hashtags`, `media_id`, `metrics` (dict), `measured_at`.

`strategy.json` keys: `version`, `updated`, `account_start_date`, `timezone`,
`brand_focus`, `cadence.{posts_per_week,max_posts_per_day}`, `tolerance_minutes`,
`jitter_minutes`, `slots[].{weekday,hour,minute,format,theme,note}`,
`content_plan{}`, `caption_strategy`, `hashtag_strategy`, `learnings`,
`next_review`.

## Brand — keep consistent if you touch content or style

- **Name:** Brine & Bloom. **Voice:** warm, knowledgeable, concise — a great cook
  talking to a friend. Minimal emoji. **Recipes must be real and accurate.**
- **Visual look** lives only in `STYLE_SUFFIX` (autopost.py) — single source of
  visual truth. Extend it there; never scatter style fragments.
- **What/when/how-often** lives only in `strategy.json` — never hard-code a
  schedule in the poster.

## Commands

```bash
pip install -r requirements.txt          # install
python -m py_compile autopost.py analyze.py   # syntax check
DRY_RUN=1 FORCE=1 python autopost.py     # full generate, prints, no publish
FORCE=1 python autopost.py               # LIVE post now — only after human confirmation
DRY_RUN=1 python analyze.py --strategize # show the proposed strategy, write nothing
python analyze.py                        # LIVE: collect metrics, adapt strategy if due
```

For local testing the human keeps keys in a gitignored `.env` and exports them.

## Environment variables / secrets

GitHub Actions repository secrets. Never hardcode, commit, print, or log values.

| Variable              | Used by            | Purpose                                   |
|-----------------------|--------------------|-------------------------------------------|
| `ANTHROPIC_API_KEY`   | autopost + analyze | content generation + strategy rewriting   |
| `REPLICATE_API_TOKEN` | autopost           | image generation                          |
| `IG_ACCESS_TOKEN`     | autopost + analyze | long-lived IG token: publish + insights + followers |
| `IG_USER_ID`          | optional override  | derived from the token if unset (`resolve_ig_user_id`) |
| `GH_PAT`              | refresh workflow   | optional; PAT with Secrets:write so refresh-token.yml can store the rotated token |

## Guardrails / conventions

- Secrets only via env vars. Keep `.gitignore` covering `.env`, `__pycache__/`,
  `venv/`. Never echo a key/token.
- **Cadence is capped by age** in `cadence_caps()` — never remove the clamp or
  let the strategist exceed it (a young automated account gets flagged).
  Hard ceilings: ≤5/wk & 1/day (<14d), ≤7/wk (<35d), ≤10/wk & 2/day after.
- **Max 5 hashtags** (`MAX_HASHTAGS`) — Instagram's 2025 cap. Don't raise it.
- Caption ≤ 2200 chars. Keep captions keyword-rich (search SEO matters more than
  hashtags now).
- **Keep post-time jitter** and varied image composition — fixed timing and
  templated images are automation flags.
- **AI-disclosure line** (`AI_DISCLOSURE`) is **temporarily OFF** (empty default)
  to A/B performance — a deliberate choice by the owner (2026-06-25). It **MUST be
  re-enabled before 2 Aug 2026**, when the EU AI Act (Article 50) transparency
  obligations for AI-generated media take effect. Do not silently re-enable early;
  do not let it slip past 2 Aug 2026.
- Recipes must be accurate and food-safe (safe marinating times/temps, no unsafe
  canning or raw-egg advice without a clear note). When unsure, be conservative.
- Fail loudly: non-zero exits on real errors so a failed run emails the human.
- **Idempotency:** posting is guarded by `slot_key` in history + the daily cap.
  Don't weaken the publish→record→commit ordering (a publish that isn't recorded
  risks a double-post next run).
- Never run a live post or live strategy write during development — use `DRY_RUN=1`.

## Definition of done

1. `python -m py_compile autopost.py analyze.py` passes.
2. `.gitignore` covers secrets and venv/cache.
3. `DRY_RUN=1 FORCE=1 python autopost.py` prints a sensible caption (≤5 hashtags,
   disclosure line) + valid public image URL(s).
4. Human added the three secrets (+ optional `GH_PAT`) and confirmed one manual live run posts correctly.
5. `strategy.json` `timezone` is set to the audience timezone.

## Extension backlog — only if the human asks

- **Reels (video)** — BUILT but intentionally DORMANT (higher per-post cost; the
  human wants time before activating). The pipeline exists: a `reel` format animates
  the Flux hero still via Veo (`generate_video`, `google/veo-3.1-fast`, image-to-video,
  ~8s) and publishes a Reel (`post_reel_to_instagram`, `media_type=REELS`); Claude
  returns a `video_prompt`. It is NOT scheduled — `strategy.json` has no reel slot, so
  the bot never auto-posts video. Trigger only manually: Run workflow → format=`reel`
  (or `FORMAT=reel` env). **To ACTIVATE recurring video:** add a slot with
  `"format":"reel"` to `strategy.json` (e.g. make one weekly slot a reel). Cost ~$1/clip.
  Do NOT activate without the human's say-so.
- **Token auto-refresh** — DONE via `refresh-token.yml` + `refresh_token.py`
  (weekly `ig_refresh_token`; needs the optional `GH_PAT` secret to store the
  rotated token).
- **Text-on-image recipe cards** (PIL/Pillow) for stronger save-bait carousels.
- **Trial Reels** (publish to non-followers only) for safe A/B testing once on
  video.
- A **review-then-post** mode that stages a draft for human approval.

## Never do

- Never enter the user's credentials, create accounts, accept terms, or solve CAPTCHAs.
- Never put secrets in code, commits, logs, or URLs.
- Never publish a live post or write a live strategy without explicit human confirmation.
- Never ship food guidance you can't vouch for as safe.
- Never remove the cadence cap, the hashtag cap, or the jitter. (The AI disclosure
  is intentionally OFF until ~2 Aug 2026 — see guardrails; re-enable it by then.)
