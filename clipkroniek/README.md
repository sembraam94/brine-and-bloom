# Clipkroniek 🎮

Autonomous gaming-clip repost account for Instagram **@clipkroniek**: reposts the
best fresh (<24h) **GTA V + VALORANT** Twitch clips — with creator credit — as
9:16 Reels, on an adaptive schedule, and learns from performance.

It's the sibling of the Brine & Bloom auto-poster: same self-optimizing loop, but
the front-half **discovers real clips** (Twitch) instead of generating images.

```
strategy.json -> clippost.py -> Instagram Reel -> Insights -> analyze.py -.
     ^                                                                      |
     '----------------------------------------------------------------------'
```

## Setup

1. **Twitch app** — [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps)
   → Register → OAuth redirect `http://localhost`, category *Application
   Integration*, *Confidential* → copy **Client ID** + **New Secret**.
2. **Instagram token** — a long-lived @clipkroniek token with
   `instagram_business_content_publish` + `instagram_business_manage_insights`
   (generate on the `brine-bloom-poster` Meta app; @clipkroniek is already a tester).
3. **Cloudflare R2** — reuse the Brine & Bloom bucket/keys (Reels need a public
   `video_url`).
4. Put all of the above in a gitignored `.env` (see `.env` template) for local
   testing, and as **GitHub Actions repository secrets** for autonomy.

## Test → go live

```bash
pip install -r requirements.txt
python -m py_compile clippost.py analyze.py twitch.py   # syntax
DISCOVER_ONLY=1 FORCE=1 python clippost.py   # pulls a real clip + writes the caption (no build/post)
DRY_RUN=1 FORCE=1 python clippost.py         # builds the full Reel, does not publish
FORCE=1 python clippost.py                   # first LIVE post (after you've reviewed a dry run)
```

Then enable the two workflows (`post.yml`, `analyze.yml`) — the poster runs every
30 min and only posts when `strategy.json` says a slot is due.

## The playbook (`strategy.json`)

- **cadence:** 7 posts/week (1/day), 1/day cap.
- **slots:** each `{weekday, hour, minute, game, region}`. Current mix: 5 Western
  + 2 Asian-source A/B probes; GTA on 4 days, VALORANT on 3.
- **regions:** `western` = `["en"]`, `asian` = Korean/Japanese/Chinese/Thai/….
- **thresholds:** `recency_hours` 24, `min_view_count`, `min/max_duration_s`.

The **geo A/B** (Western vs Asian-language source clips) is baked into the slots;
`analyze.py` prints reach + follows/shares-per-reach by region and game so you can
see which converts and shift the mix.

## Direction

**"A" — revive-and-optimize-for-reach.** A prior manual run pulled 332K reach but
~0 follows (classic reach-without-identity for a faceless repost page). This cycle
fixes consistency + niche and runs the geo A/B. The higher-ceiling upgrade
(**Direction B**: an AI-commentator persona for a real follow-through moat) is on
the backlog and reuses Brine & Bloom's TTS/mascot stack.
