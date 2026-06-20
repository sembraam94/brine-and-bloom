# Brine & Bloom — self-optimizing Instagram cooking account

A hands-off Instagram account for cooking tips, recipes, and marinades with
AI-styled ingredient photography — that **learns what works and adapts**. It
doesn't just post on a fixed schedule; it strategizes how to grow fastest,
reviews how every post actually performed, and rewrites its own plan to do more
of what's working.

**The loop:**

```
   strategy.json ──▶ autopost.py ──▶ Instagram ──▶ Insights ──▶ analyze.py ──┐
   (the playbook)    (posts when      (reach,        (measures)   (rewrites    │
        ▲             the plan says)   saves,                       strategy)   │
        └────────────────────────────  shares…) ───────────────────────────────┘
```

Once set up it runs itself on **GitHub Actions** (free, no server). There's a
**one-time setup only you can do** (creating accounts, getting API keys) because
it involves your credentials. Budget about an hour. After that it's autonomous,
with light upkeep (see Maintenance).

---

## What's new vs. a plain auto-poster

It now has a brain *and* a feedback loop:

- **Strategizes up front.** `strategy.json` is a living playbook: how many posts
  per week, which days/times, what format, and what to post in each slot.
- **Picks timing & cadence for growth.** Schedule and frequency are data, not
  hard-coded — the strategist rewrites them. Starts conservative and **ramps up
  safely** as the account ages.
- **Reviews every post.** `analyze.py` pulls each post's real Insights (reach,
  saves, shares, views, follows) and tracks follower growth over time.
- **Adapts.** Weekly, Claude reads the numbers and rewrites the strategy —
  shifting toward the themes, formats, and posting times that actually perform,
  and updating the "learnings" that every future caption is written against.
- **Stays safe.** Hard caps on cadence, jittered post times, a 5-hashtag limit,
  and a plain AI-disclosure line keep a young automated account out of trouble.

---

## How it works (data flow)

**Poster** (`autopost.py`, runs hourly): each run checks "is *now* one of the
slots in `strategy.json`, and have I not already posted it?" If no → exits in
~30s. If yes → Claude writes the post → Flux renders the image(s) → it publishes
via the Graph API → logs the post (with its media id) to `history.json`.

**Analyzer** (`analyze.py`, runs daily): reads the live follower count into
`followers.json`, pulls Insights for recent posts into `history.json`, and once
a week asks Claude to rewrite `strategy.json` toward what's working.

Both jobs commit their state files back to the repo. Those commits double as the
repo activity that stops GitHub from auto-disabling the schedule.

---

## Files

```
autopost.py                     # the poster (brand config at the top)
analyze.py                      # the analyzer + strategist
strategy.json                   # THE PLAYBOOK — timing, cadence, themes, learnings (auto-rewritten)
history.json                    # post log + per-post metrics (auto-created)
followers.json                  # daily follower count timeline (auto-created)
requirements.txt                # deps: requests, tzdata
.github/workflows/post.yml      # hourly poster
.github/workflows/analyze.yml   # daily analyzer
.gitignore
README.md                       # this file
CLAUDE.md                       # notes for Claude Code working in the repo
```

---

## What you'll need

The same **four credentials**, stored as GitHub repository secrets (never in code):

| Secret name           | What it is                              | Where to get it |
|-----------------------|-----------------------------------------|-----------------|
| `ANTHROPIC_API_KEY`   | Claude — writes posts & the strategy    | console.anthropic.com |
| `REPLICATE_API_TOKEN` | Flux — the image generator              | replicate.com → account → API tokens |
| `IG_USER_ID`          | Your Instagram professional account ID  | Meta setup (below) |
| `IG_ACCESS_TOKEN`     | Long-lived token to post on your behalf | Meta setup (below) |

*(No new secrets were added for the growth/analytics features — the same token
that publishes also reads Insights and the follower count.)*

---

## Step 1 — Instagram + Meta (the fiddly part; do this first)

Instagram only allows automated posting through Meta's official Graph API, and
only for **professional** accounts. Personal accounts cannot post via the API.

1. **Make the account.** Create the Instagram account (or use a fresh one). In
   the app: Settings → switch to a **Professional account** → choose
   **Business**.
2. **Link a Facebook Page.** Create a Facebook Page and connect your Instagram
   account to it. The API requires this link even though you'll never really use
   the Page.
3. **Create a Meta developer app.** developers.facebook.com → My Apps → Create
   App → add the **Instagram** product.
4. **Get your token and ID.** In Meta's Graph API Explorer (or the Instagram
   Platform setup flow), request these permissions and generate a token:
   `instagram_basic`, `instagram_content_publish`, and
   `instagram_manage_insights` (the last one is needed for the analytics loop —
   reading post Insights). Then exchange it for a **long-lived** token (~60
   days). Grab your Instagram account's numeric ID at the same time.
   Meta's guide: https://developers.facebook.com/docs/instagram-platform/content-publishing/

   > **You can skip the 2–4 week "App Review."** That review is only required to
   > post to accounts you *don't* own. For your own account, add yourself as a
   > tester / keep the app in development mode and it works immediately.

Put the numeric ID in `IG_USER_ID` and the long-lived token in `IG_ACCESS_TOKEN`.

## Step 2 — Anthropic key

Sign in at console.anthropic.com, create an API key, add a little credit. This
is `ANTHROPIC_API_KEY`.

## Step 3 — Replicate token

Sign in at replicate.com → account settings → API tokens → create one → add a
payment method. This is `REPLICATE_API_TOKEN`.

## Step 4 — Put it on GitHub

1. Create a new GitHub repo. **Public is recommended** — Actions minutes are
   unlimited on public repos, and the hourly poster otherwise uses ~700–800 of
   your 2,000 free private minutes/month. (There are no secrets in the code, so
   public is safe; secrets live only in GitHub's encrypted store.)
2. Push all the files (the layout above — keep `post.yml`/`analyze.yml` under
   `.github/workflows/`).
3. **Settings → Secrets and variables → Actions → New repository secret** → add
   all four secrets from the table (exact, case-sensitive names).

## Step 5 — Set your audience timezone (important)

Open `strategy.json` and set `"timezone"` to your **audience's** timezone (IANA
name, e.g. `America/New_York`, `Europe/Amsterdam`, `Europe/London`). The seed
slots are tuned for a US food audience; if your audience is European, set the
timezone and the strategist will adjust the slot hours from there. DST is handled
automatically — you set a timezone, not a UTC offset.

## Step 6 — Test before going live

1. Actions tab → **Brine & Bloom — hourly poster** → **Run workflow**. With no
   slot due it will just no-op (that's expected).
2. To test the full generate-and-post pipeline on demand, temporarily uncomment
   `DRY_RUN: "1"` (and `FORCE: "1"`) in `.github/workflows/post.yml`, run the
   workflow by hand, and watch it generate a caption + image and *print* what it
   would post without publishing. Check the image URL and caption look good.
3. Happy? Re-comment `DRY_RUN` (leave `FORCE: "1"`), run once more — **this one
   posts for real.** Confirm it shows up on the account. Then re-comment `FORCE`.

## Step 7 — Leave it alone

The hourly poster now posts on the strategy's schedule, and the daily analyzer
reviews performance and adapts. That's it.

---

## Growth strategy & safety (how it grows the page)

This is tuned to 2026 Instagram reality:

- **Ramp, don't blast.** A brand-new account is watched closely for bot
  behavior. It **starts at 4 posts/week** and the safety caps widen with age:
  ≤5/week & 1/day for the first 2 weeks, ≤7/week to ~5 weeks, then up to ~10/week
  & 2/day. The strategist can move *within* those caps but never past them.
- **Jittered timing.** Posting at a fixed clock minute looks scripted, so each
  post fires at a slightly random time within its window.
- **Optimizes for the real growth signals.** In 2026 the algorithm rewards
  **sends/shares > saves > watch-time/likes**, and reach to *non-followers* is
  what grows a young account. Every caption is pushed toward "save this" / "send
  this to a friend," and the strategist re-weights toward posts with high
  sends- and saves-per-reach.
- **Captions over hashtags.** Instagram caps posts at **5 hashtags** (since Dec
  2025) and now reads captions for search. Captions lead with a keyword-rich,
  searchable first line; hashtags are a light 3–5-tag supplement, varied per post.
- **AI disclosure.** Every caption carries a short "AI-styled photography · the
  recipe is real" line. This keeps you ahead of Meta's AI auto-labeling and
  satisfies the EU AI Act's transparency rules (Article 50, from 2 Aug 2026).
  The recipes are real — only the imagery is generated.

> **Honest limitation:** the single biggest 2026 growth lever is **Reels**
> (video), and this system posts **images and photo-carousels**, not video.
> That's a deliberate scope choice — automated, on-brand video generation is a
> much bigger build. Images + carousels grow a food page well; if you later want
> Reels, it's the top item in CLAUDE.md's extension backlog.

---

## Costs (rough — check current pricing)

- **Claude:** a fraction of a cent per post + one small strategy rewrite/week.
  Negligible.
- **Flux image:** a few cents per image. Single posts ~1 image; carousels 2–4.
  Call it ~$1–4/month at this cadence.
- **Instagram API & GitHub Actions:** free (Actions free on public repos).

Roughly the price of a coffee or two per month, mostly the images.

---

## Maintenance (the "light upkeep" part)

- **Token refresh (~every 60 days).** Meta's long-lived token expires. When runs
  start failing with an auth error (code 190), mint a fresh long-lived token
  (Step 1) and update the `IG_ACCESS_TOKEN` secret. Because the analyzer makes
  an authenticated call **every day**, an expired token surfaces within a day via
  a red, emailed run — you won't silently go dark for long.
- **Monitoring is automatic.** A failed run emails you and shows red in Actions.
- **The schedule stays alive by itself.** Daily analyzer commits + per-post
  commits count as activity, so GitHub won't auto-disable the schedule.
- **Replace timing with real data.** Once you pass ~100 followers, Instagram
  unlocks audience/demographic insights; the strategist already leans on your
  actual per-slot performance well before that.

---

## Make it yours

- **Brand & look:** `STYLE_SUFFIX`, `BRAND_NAME`, and the voice in
  `call_claude()`'s system prompt — all at the top of `autopost.py`.
- **The plan:** everything about *when/how often/what* lives in `strategy.json`.
  You can hand-edit it to seed the strategy; the analyzer takes over from there.
- **Cadence safety caps:** `cadence_caps()` in `analyze.py`.
- **Image shape:** `aspect_ratio` in `generate_image()` (`"1:1"` or `"4:5"`).

## Troubleshooting

- **"Missing required environment variable"** — a secret name doesn't match
  (case-sensitive).
- **`strategy.json is missing or has no slots`** — make sure `strategy.json` is
  committed at the repo root.
- **Instagram container `ERROR`/`EXPIRED`** — usually the image URL wasn't
  publicly fetchable or wasn't JPEG. Flux URLs are public JPEGs; if you switch
  providers you need a public JPEG host.
- **Auth / token errors (code 190)** — `IG_ACCESS_TOKEN` expired; refresh it.
- **Insights look empty for a brand-new post** — normal; the analyzer waits ~18h
  before measuring and refreshes for two weeks.
- **Hit the publish cap** — the poster reads `content_publishing_limit` and skips
  if you're at the 100-posts/24h ceiling (you won't be at 1/day).
- **Schedule drift** — GitHub's scheduled runs can fire several minutes late; the
  90-minute forward tolerance window absorbs it.
