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

1. **Discover** — Twitch Get Clips for the slot's game, window = last
   `recency_hours` (24h), filtered to the slot's **region by clip `language`**
   (western=`en`, asian=`ko/ja/zh/...`), min views/duration, deduped vs history,
   ranked by `view_count`.
2. **Download** — `yt-dlp` the top clip to mp4.
3. **Reformat** — ffmpeg → 9:16 (blurred fill + centered gameplay) + a burned-in
   UPPERCASE **hook** banner near the top.
4. **Caption** — Claude (`claude-sonnet-4-6`) writes hook + caption + hashtags;
   `assemble_caption` adds the **creator credit** + follow CTA.
5. **Host + publish** — upload mp4 to **R2** (Reels need a public `video_url`) →
   `post_reel_to_instagram` (media_type=REELS → poll status → media_publish) →
   record to history.json → delete the R2 object.

`analyze.py` (daily): follower count → followers.json; per-Reel Insights →
history.json; prints the **A/B readout** (by region / game / region|game); weekly
(or `--strategize`) refreshes `strategy.json` `learnings` via Claude. The slot
GRID is tuned by a human from the readout, not auto-rewritten.

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
| `IG_ACCESS_TOKEN` | @clipkroniek token — needs `instagram_business_content_publish` (+ insights) |
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
- Token auto-refresh (weekly `ig_refresh_token`, needs a `GH_PAT` secret).
