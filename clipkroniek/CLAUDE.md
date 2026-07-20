# CLAUDE.md — Clipkroniek

Context for Claude Code working in this repo. Read fully before acting.

## What this is

An **autonomous gaming-clip repost account** for Instagram **@clipkroniek**. It
reposts the best FRESH (<24h) **GTA V + VALORANT** Twitch clips — with creator
credit — as 9:16 Reels, on a strategy-defined schedule, then measures each post
and adapts. Sibling of the Brine & Bloom project (same loop, different front-half:
discover real clips instead of generating images). Scheduler = GitHub Actions.

Direction chosen 2026-07-14: **"A" (revive-as-is, optimize for reach)** — a prior
manual run got 332K reach but ~0 follows (reach-without-identity). This cycle
fixes the two cheap levers: **consistency** and a **tight GTA+VALORANT niche**,
plus a **geo A/B** (Western vs Asian-language source clips) to learn what converts.

## The loop

```
strategy.json -> clippost.py -> Instagram Reel -> Insights -> analyze.py -.
 (playbook)      (discover+post                     (measure)  (learnings) |
     ^            when a slot is due)                                       |
     '-----------------------------------------------------------------------'
```

## Flow (clippost.py, per due slot)

1. **Discover a POOL** (`discover_clip`) — Twitch Get Clips for the slot's game,
   window = last `recency_hours` (24h), filtered to region by clip `language`,
   min views/duration, deduped vs history. Ranked into a **pool** (not one pick) by
   a log-domain score: **velocity** (views/hour, #6) × **duration band** (8–30s
   sweet spot, #11) × **broadcaster-recency penalty** (×0.3 <3d / ×0.6 <7d, #9) ×
   **curated boost** (`curated_boost`, #9). Org/tournament channels
   (`prune_broadcasters`) are dropped. If nothing qualifies, an **escalation ladder**
   widens 24h→48h→(western) more EU langs→best-available so the account never goes
   dark. Asian slots also top up from `asian_streamers` (#10).
2. **Judge + write** (`call_claude`, `claude-sonnet-4-6`, #7) — the pool's top-8
   METADATA (no video) goes to Claude, which PICKS the most viral-looking gameplay
   clip (skips reaction/gambling/drama titles) and writes the SEO caption (#19a),
   generic hook, niche hashtags, and a first-comment question (#4) in one call.
   `learnings` + `caption_strategy` are fed in as advisory (#16). Falls back to the
   top-ranked clip on any error.
3. **Download with fallback** (#22a) — `yt-dlp` the pick; on failure try the next
   pool candidate; exit non-zero only if ALL fail.
4. **Reformat** (`reformat_reel`) — ffmpeg → 9:16 (blurred fill + a **zoom-cropped**
   gameplay overlay, per-game `fg_zoom`, #13). **Facecam detection** (`vision.py`,
   one **Claude Vision** call on a single frame, reuses `ANTHROPIC_API_KEY`): if the
   streamer's webcam is found, the reel **STACKS** the facecam above the gameplay
   (both fully visible, not overlaid) instead of cropping it off; none found →
   standard layout. Gated on `strategy.facecam_stack.enabled` (default true), fully
   non-fatal. Optional **smart-trim to the audio peak** (#12, an A/B via
   `smart_trim.mode:"ab"` — ~50/50 by slot hash); when **transcription** is on
   (`transcribe.py`: Groq `whisper-large-v3-turbo` primary + self-hosted
   `faster-whisper` fallback, needs `GROQ_API_KEY`), the transcript's verbal-reaction
   moment REFINES the cut — audio peak leads, blended 60/40 when they agree, ignored
   when they diverge or the clip has no speech. Permanent **@clipkroniek
   watermark** + a last-2.5s **FOLLOW CTA** (#1). **loudnorm** (#15), **60fps cap**
   (#14), slow/crf18 encode. (Compilations keep the zoom-crop.) **Animated captions**
   (`captions.py`): builds an ASS subtitle file from the transcript's word timestamps
   and burns it via ffmpeg libass (Anton font bundled in `fonts/`, Noto Sans CJK
   auto-installed for JP/KR) — 1-3 UPPERCASE words, the spoken word pops + highlights
   yellow in sync; on for all clips (`strategy.captions`). Plus a small **streamer
   credit** burned top-left (`@twitch-login`).
5. **Cover** (`build_cover`, #3) — a frame at the action peak in the 9:16 look with
   `CLIPKRONIEK #N — GAME` burned into the grid-safe centre. Episode number is on
   the COVER ONLY (never the caption). `cover_url` with a `thumb_offset` fallback.
6. **Host + publish** — upload mp4 (+ cover jpg) to **R2** →
   `post_reel_to_instagram` → record to history.json → **first comment**
   (`post_comment`, #4, non-fatal) → **YouTube Shorts cross-post** (`crosspost_youtube`,
   optional/non-fatal — reuses the same reel) → delete the R2 objects. `sweep_r2_orphans`
   cleans up crash-orphaned objects at the start of each live run (#22c).

**Weekly Top-3** (`post_top3`, #8): `FORMAT_OVERRIDE=top3` (Sunday 11:30 UTC cron or
manual dispatch) re-cuts the week's best 3 clips into one ranked `#1/#2/#3` reel.

`analyze.py` (daily): follower count → followers.json; per-Reel Insights (now incl.
`profile_visits`) → history.json, with a `metrics_24h` snapshot for like-with-like
A/B; prints the **funnel readout** (reach→visits→follows, shares/saves/**retention**
per reach, by region / game / **hour** / **trim** / format over a 28-day window);
weekly (or `--strategize`) refreshes `learnings` + `game_hashtags` via Claude and
runs the game **rotation**. If the newest post is older than `went_dark_hours` (48h)
the analyzer **exits non-zero** to alarm the owner (#21). The slot GRID is still
tuned by a human from the readout, not auto-rewritten.

## Human-in-the-loop review (Telegram, optional)

When `strategy.human_review.enabled` AND both `TELEGRAM_*` secrets are set, the poster
does NOT post at a due slot — it **PROPOSES**: discovers the pool, hosts the top-N
(`candidates`, default 3) raw clips on R2, sends them as playable videos to the owner's
Telegram, and writes a pending record to R2 (`review/pending-<slot_key>.json`: the
candidate clips, a baseline Telegram update_id, and a `respond_window_min` deadline).
It does not touch history. A separate **FULFILL** cron (`clipkroniek-review-fulfill.yml`,
`REVIEW_FULFILL=1`, every ~10 min) reads the owner's reply (`"<1/2/3> <one-line
description>"`, latest valid pick wins) and builds+posts THAT clip — the one-liner is
passed to `call_claude(chosen_index=…, human_desc=…)` to ground a SPECIFIC caption (the
one case where naming the moment is allowed). If the window passes with no reply, it
auto-posts (Claude picks among the candidates) so the slot is never missed.

Design guarantees: **fallback-first** — any failure (no R2, Telegram send fails, <2
previewable clips, can't save pending) posts autonomously right then. **At most one
pending** (`_pending_exists()` blocks overlap). **Idempotent** — fulfill skips a slot
already in history; both workflows share the `ck-state` concurrency group so posting/
history are never concurrent. `force`/`dry`/`discover_only` and the Top-3/longform
overrides bypass review. Code: `propose_review` / `fulfill_reviews` / `_produce_and_post`
(the shared build+publish body) in clippost.py; `telegram.py` (send + poll); one-time
`telegram_setup.py` prints your chat_id. Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

## Files

```
clippost.py     # poster: discover -> download -> reformat -> caption -> publish
twitch.py       # Twitch Helix helpers (app token, resolve game, get clips)
analyze.py      # analyzer: insights + A/B readout + weekly learnings
strategy.json   # THE PLAYBOOK: tz, cadence, slots{weekday,hour,minute,game,region}, regions, games, thresholds
history.json    # auto-created: post records + metrics — do NOT hand-edit
followers.json  # auto-created
.github/workflows/post.yml     # every 30 min: gate, discover, post, commit
.github/workflows/analyze.yml  # daily: metrics + learnings, commit
```

## Env / secrets (never hardcode, commit, print, or log)

| Var | Purpose |
|-----|---------|
| `ANTHROPIC_API_KEY` | caption + learnings |
| `IG_ACCESS_TOKEN` | @clipkroniek token — needs `instagram_business_content_publish` (+ insights). The first-comment feature (#4) also needs `instagram_business_manage_comments`; if absent the comment 400s and is skipped (non-fatal). Wired from the `CK_IG_ACCESS_TOKEN` secret. |
| `GH_PAT` | optional; PAT with Secrets:write so `refresh-token.yml` can store the rotated token (fails loudly without it) |
| `FORMAT_OVERRIDE` | `top3` triggers the weekly compilation (Sunday cron / manual) |
| `YT_CLIENT_ID` / `YT_CLIENT_SECRET` / `YT_REFRESH_TOKEN` | optional YouTube Shorts cross-post (`youtube.py`). No-op unless all three are set. Minted once via `youtube_auth.py`; see the YouTube section below. |
| `GROQ_API_KEY` | optional clip transcription (`transcribe.py`) that assists the cut-moment choice. Free (no card). Without it, STT is off (audio-peak only) unless `transcribe.self_host_standalone`. Self-hosted `faster-whisper` is the fallback. |
| `IG_USER_ID` | optional; derived from token |
| `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` | Twitch app (client-credentials) for clip discovery |
| `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / `R2_BUCKET` / `R2_PUBLIC_BASE_URL` | host the mp4 (reuse Brine & Bloom's R2) |

Local dev keeps these in a gitignored `.env`.

## Commands

```bash
pip install -r requirements.txt
python -m py_compile clippost.py analyze.py twitch.py     # syntax
DISCOVER_ONLY=1 FORCE=1 python clippost.py   # local smoke test (needs ANTHROPIC + TWITCH only)
DRY_RUN=1 FORCE=1 python clippost.py         # full build, no publish (needs ffmpeg + yt-dlp)
FORCE=1 python clippost.py                   # LIVE post now — only after human confirmation
DRY_RUN=1 python analyze.py --strategize     # show proposed learnings, write nothing
```

## Guardrails / conventions

- Secrets only via env. `.gitignore` covers `.env`, `*.mp4`, caches.
- **Rights posture (repost account):** always credit `broadcaster`/`creator`;
  clips ≤ `max_duration_s`; Twitch clips have no foreign watermark (good). This is
  Direction A — reach-optimized reposting; the DMCA/originality risk is accepted
  and managed, not eliminated. Do not add copyrighted-music overlays.
- **Material edit (Meta April-2026 originality rules):** the @clipkroniek watermark,
  the trim, the zoom-recompose and the branded cover are what make each reel a
  "material edit" rather than a bare aggregator repost. Do not strip the watermark
  or ship unbranded reels — `brand_watermark:true` aborts the build if no font can
  be installed rather than posting unbranded.
- **Episode numbering is COVER-only.** `#N` may appear on the cover and in the
  first comment, NEVER in the caption (owner decision — serialized captions were
  explicitly rejected).
- **Smart-trim is an A/B, not a default.** `smart_trim.mode:"ab"` splits ~50/50 by
  slot hash; the analyzer reads the `trimmed` flag. Do not flip it to `"always"`
  until the readout shows trimmed posts win on views — that's the owner's call.
- **Idempotency:** posting is guarded by `slot_key` (date-weekday-hour) in history
  + the daily cap + clip-id dedupe. Keep the publish→record→commit order.
- **Fail loudly** (non-zero exit) so a failed Actions run emails the owner.
- Never publish a live post without explicit human confirmation; use DRY_RUN /
  DISCOVER_ONLY in development.
- Keep the geo A/B intact until the analyzer shows a clear region winner.

## Backlog (only if the human asks)

- **Direction B upgrade** — add an AI-commentator persona (reuse Brine & Bloom's
  TTS + mascot + burned-caption stack) for a real follow-through moat.
- More sources (Reddit r/gtaonline, Kick), more games, per-clip trending audio.
- **Token auto-refresh — DONE** (`refresh-token.yml` matrixes IG_ACCESS_TOKEN +
  CK_IG_ACCESS_TOKEN weekly; needs the `GH_PAT` secret to store the rotated token,
  else it fails loudly).

## Owner actions (one-time, to unlock the full upgrade)

1. **Add the `GH_PAT` secret** (fine-grained PAT, Secrets: Read+write on
   sembraam94/brine-and-bloom) or both IG tokens expire in ~60 days.
2. **Regenerate `CK_IG_ACCESS_TOKEN` with `instagram_business_manage_comments`**
   so the auto first-comment (#4) posts; without it the comment is skipped (the
   reel still publishes fine).
3. **One-time bio rewrite** on @clipkroniek to state the identity ("Daily best-of
   GTA & VALORANT clips 🎮 new clip every day") — the reach fix only converts to
   follows if the profile says who to follow.
4. **Review the trim A/B after ~2 weeks** (`analyze.py` readout `trim:on/off`
   cells). If trimmed wins on views, set `smart_trim.mode:"always"`.
5. **Watch the game rotation** — the news scanner can propose single-player launch
   waves (e.g. an AC remaster); glance at the weekly `[rotation]` log line before
   trusting a swap.

## Clip performance tracker (`clip_tracker.py`)

A standalone data-collection subsystem (separate from the poster): every ~30 min
(`clipkroniek-tracker.yml`, cron `12,42 * * * *`) it registers the top FRESH Twitch
clips — per configured game AND across the current top games (platform-wide sweep,
`twitch.get_top_games`) — and snapshots each clip's `view_count` at age milestones
(`strategy.tracker.milestones_h` = 0.5/1/1.5/2/3/4/8/12/14/16/18/20/22/24h, via `twitch.get_clips_by_id`),
building a dataset of how clips DEVELOP over their first day (does early velocity
predict virality → a sharper selection signal than one snapshot). State + dataset
live in **R2**, not git: `tracker/tracking.json` (active clips in their 24h window) +
`tracker/dataset-<date>.jsonl` (completed trajectories, one clip/line). $0 (Twitch +
public-repo Actions free). Config: `strategy.tracker`. Dataset URL:
`<R2_PUBLIC_BASE_URL>/tracker/dataset-<date>.jsonl`.

## YouTube Shorts cross-post (optional)

`youtube.py` uploads the same reel to the Clipkroniek YouTube channel as a Short,
right after the IG publish. It's **off until three secrets exist** and never fatal.

**YouTube measurement** (`analyze.py measure_youtube`): the daily analyzer pulls each
cross-posted video's stats into `record.youtube.metrics` (Data API `videos.list`
statistics + Analytics API `reports.query`: watch time, `averageViewPercentage`
= retention, `subscribersGained`) and prints a `yt:game/format` readout — the seed of
an eventual IG-vs-YT comparison. It needs the token re-minted with **read** scopes:
`youtube.readonly` + `yt-analytics.readonly` (the upload-only token 403s → the run
just logs the re-mint hint and no-ops, so it's safe already). To activate: add those
two scopes on the OAuth consent screen, re-run `youtube_auth.py` (its `SCOPE` already
requests all three), update `YT_REFRESH_TOKEN`, and add the `YT_*` secrets to the
analyzer workflow. Numbers stay ~0 until the videos are **public** (a private video
has no audience).

**One-time setup:**
1. Google Cloud project → enable **YouTube Data API v3**.
2. **OAuth consent screen** → External → add scope `.../auth/youtube.upload` →
   **PUBLISH to Production** (⚠️ Testing mode expires the refresh token after 7 days
   for this sensitive scope).
3. **Credentials** → OAuth client ID → **Desktop app** → get Client ID + Secret.
4. Run `youtube_auth.py` locally once (`YT_CLIENT_ID=… YT_CLIENT_SECRET=… python
   youtube_auth.py`) → authorize on the Clipkroniek channel (click through the
   "unverified app" warning) → it prints the refresh token.
5. Add repo secrets `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN`.

**Known limit — the private lock:** an **unaudited** Cloud project forces every
API upload to `privacyStatus: private` regardless of what we request. The poster
requests `public` (`strategy.youtube.privacy`) and logs a NOTE if YouTube overrides
it to private. To get hands-off **public** Shorts, complete YouTube's **API
compliance audit** (separate from OAuth verification); until then, uploads land
private and must be flipped to public in YouTube Studio. Verified facts: `videos.insert`
uses a dedicated **100/day** quota bucket; a Short is classified automatically by
9:16 aspect + ≤180s (no `#Shorts` needed); title ≤100 chars with `< >` stripped;
`selfDeclaredMadeForKids` is always sent. The `check_token.py`-style health check
doesn't cover YouTube — a failed upload just logs and the IG post is unaffected.

## Weekly long-form compilation (`longform.py`) — DORMANT until enabled

A ranked long-form YouTube best-of: intro card → `[ #N card → clip ]` worst→best
(#1 last) → outro card, assembled **16:9** and uploaded as long-form (not a Short).
This is the transformative, watch-time format that clears the reused-content bar the
daily Shorts can't. **YouTube-only** (a ~10-min video can't be an IG Reel). Triggered
by `FORMAT_OVERRIDE=longform` (Sunday 12:30 UTC cron + a dispatch choice).

**Off by default** (`strategy.longform.enabled=false`); the cron no-ops until enabled.
Three stacking activation levels — each layer needs its flag AND its key, and degrades
gracefully if absent:

1. **Silent countdown** — set `longform.enabled=true`. Needs only the existing YT
   secrets (+ R2). Text cards + clips, no voice. Zero new dependencies.
2. **+ AI voiceover** — also set `longform.voiceover=true` and add `REPLICATE_API_TOKEN`.
   `tts.py` narrates each card (Kokoro by default; override via `TTS_MODEL`/`TTS_VOICE`).
   Narration is on the CARDS; clips keep their own audio (no ducking/sync).
3. **+ Gemini-grounded narration** — also set `longform.gemini_grounding=true` and add
   `GEMINI_API_KEY`. `gemini.py` watches each trimmed clip at `gemini_fps` (default 10)
   and returns a one-line description so the narration is specific, not generic. ~cents.

Config: `strategy.longform.{clip_count, days, seg_max_s, card_seconds, voiceover,
gemini_grounding, gemini_fps, privacy, title}`. Idempotent per ISO week
(`longform-<year>-W<week>` slot_key). Records `format:"longform"`.

**Caveats:** captions v1 = the on-card text (rank/streamer/one-line desc), not a
word-for-word SRT track — a full timed SRT (Whisper the final mix → captions.insert)
is a follow-up. A source clip with **no audio track** would break the concat (Twitch
clips always have audio; documented, fails loudly). First live run needs ≥ `clip_count/2`
real clips banked in the last 7 days, so it self-skips until the account has history.
