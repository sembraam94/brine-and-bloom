#!/usr/bin/env python3
"""
Tracker dataset analysis — does EARLY view velocity predict the 24h outcome?

Reads every `tracker/dataset-<date>.jsonl` the clip tracker has written to R2 and
answers the owner's question ("is there a pattern in views in the first 15/30/60
minutes?") RIGOROUSLY, defending against the two traps baked into how the data is
collected:

  1. SURVIVORSHIP BIAS. Non-control clips are pruned at 1.5h by their 1.5h views, so
     clips that *reach* 24h are conditioned on a high early count. Computing an
     early->24h correlation over those survivors is circular. => The headline is
     computed ONLY on the CONTROL sample (mid-ranked clips, never pruned, tracked the
     full 24h regardless of early velocity). Control is range-restricted (mid-rank),
     so its correlation is a CONSERVATIVE LOWER bound — stated as such.
  2. CROSS-SCALE MIXING. Games differ in view scale by orders of magnitude, so every
     pooled statistic uses WITHIN-GAME rank normalization, never raw pooled views.

Nothing is asserted below hard min-n gates: below the global floor the script prints
one honest "not enough data yet" line and exits 0. Every reported number carries n
and a CI (or "insufficient data"); any statistic whose CI includes the random
baseline is rendered "not yet distinguishable from chance".

Output: a markdown readout to stdout AND to R2 `tracker/readout-latest.md`, plus a
machine-readable `tracker/readout-latest.json` sidecar the selection code can consume.

Runs on GitHub Actions (R2 secrets live there). $0. Pure stdlib + numpy + boto3.
"""
import os
import re
import sys
import json
import math
import datetime
from collections import defaultdict

import numpy as np

from clippost import env, _r2_client, r2_configured, now_utc, _parse_ts

# ---- config / gates (from the methodology spec) ----------------------------
SEED = 1234
EARLY_MS = [0.5, 1.0]            # predictor milestones ("first 30/60 min")
OUTCOME_M = 24.0
OUTCOME_TOL = 2.0               # |age_h - 24| <= 2h
OK_REASONS = {"24h", "extended_bumped", "extended_end"}   # carry a full 24h trajectory

FLOOR_USABLE = 25              # total clips with a valid 24h outcome
FLOOR_CONTROL24 = 10          # control clips with a valid 24h outcome
MIN_PAIRS_RHO = 30            # pooled n before a correlation is asserted
MIN_CELL_SELECT = 8          # top-early cell count for the selection test
BOOT_B = 2000
PERM_P = 10000
BH_Q = 0.10

# USABLE-milestone gate
G_RHO = 0.40
G_LIFT = 1.30
G_LOO = 0.15                  # leave-one-out rho drift tolerance

DATASET_PREFIX = "tracker/dataset-"
DATE_RE = re.compile(r"tracker/dataset-\d{4}-\d{2}-\d{2}\.jsonl$")
READOUT_KEY = "tracker/readout-latest.md"
SIDECAR_KEY = "tracker/readout-latest.json"


# ==========================================================================
# Loading
# ==========================================================================
def _list_dataset_keys(client, bucket):
    keys, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": DATASET_PREFIX, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        resp = client.list_objects_v2(**kw)
        for obj in resp.get("Contents", []):
            if DATE_RE.search(obj["Key"]):
                keys.append(obj["Key"])
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
        else:
            break
    return sorted(keys)


def load_dataset(client, bucket):
    """Return (records, stats) — every clip trajectory across all day-files."""
    keys = _list_dataset_keys(client, bucket)
    records, raw_lines, malformed = [], 0, 0
    for key in keys:
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            raw_lines += 1
            try:
                records.append(json.loads(line))
            except Exception:
                malformed += 1
    dates = sorted(k[len(DATASET_PREFIX):-6] for k in keys)
    stats = {"files": len(keys), "raw_lines": raw_lines, "malformed": malformed,
             "date_range": (dates[0], dates[-1]) if dates else (None, None)}
    return records, stats


def load_active(client, bucket):
    """The in-flight clips (tracker/tracking.json) — still being tracked, not yet
    archived. They carry the early snapshots (0.5/1/1.5h) even though they have no 24h
    completion, so they belong in the EARLY-development analysis (and include the top
    survivors + control clips that the archived dataset doesn't have yet)."""
    try:
        body = client.get_object(Bucket=bucket, Key=STATE_KEY)["Body"].read().decode("utf-8")
        state = json.loads(body)
    except Exception:
        return []
    recs = []
    for cid, rec in (state.items() if isinstance(state, dict) else []):
        r = dict(rec)
        r.setdefault("clip_id", cid)
        recs.append(r)
    return recs


def dedupe_clips(records):
    """Keep, per clip_id, the record with the MOST snapshots (tie: latest completed_at)."""
    best = {}
    for r in records:
        cid = r.get("clip_id")
        if not cid:
            continue
        cur = best.get(cid)
        if cur is None:
            best[cid] = r
            continue
        nk = len(r.get("snapshots") or [])
        nc = len(cur.get("snapshots") or [])
        if nk > nc or (nk == nc and str(r.get("completed_at")) > str(cur.get("completed_at"))):
            best[cid] = r
    removed = len(records) - len(best)
    return list(best.values()), removed


# ==========================================================================
# Milestone / predictor / outcome extraction
# ==========================================================================
def get_milestone(rec, target_h, tol):
    """Views + real age at the snapshot bucketed to `target_h`, validated against the
    age-tolerance window. Returns (views, age_h) or None (MISSING — never 0)."""
    cands = [s for s in (rec.get("snapshots") or []) if s.get("target_h") == target_h]
    if not cands:
        return None
    s = min(cands, key=lambda s: abs((s.get("age_h") or 1e9) - target_h))
    age = s.get("age_h")
    views = s.get("views")
    if age is None or age <= 0 or abs(age - target_h) > tol:
        return None
    if views is None or views < 0:
        return None
    return float(views), float(age)


def _early_tol(target_h):
    return max(0.25, 0.5 * target_h)   # 0.5h -> 0.25..0.75 ; 1h -> 0.5..1.5


def early_velocity(rec, target_h):
    m = get_milestone(rec, target_h, _early_tol(target_h))
    if not m:
        return None
    views, age = m
    return views / age            # age-normalized (absorbs target_h vs age_h jitter)


def outcome_y24(rec):
    if rec.get("reason") not in OK_REASONS:
        return None
    m = get_milestone(rec, OUTCOME_M, OUTCOME_TOL)
    return m[0] if m else None


def reconstruct_cohort(rec):
    """Approximate 30-min registration bucket = completed_at - max(age_h), floored."""
    done = _parse_ts(rec.get("completed_at"))
    ages = [s.get("age_h") for s in (rec.get("snapshots") or []) if s.get("age_h") is not None]
    if done is None or not ages:
        return rec.get("game_id") or "?"     # fall back to a coarse cluster
    start = done - datetime.timedelta(hours=max(ages))
    epoch_min = int(start.timestamp() // 60)
    return epoch_min - (epoch_min % 30)


# ==========================================================================
# Populations
# ==========================================================================
def partition(clips):
    control, hot, all_early, excluded = [], [], [], []
    inv = defaultdict(int)
    for r in clips:
        inv["total"] += 1
        inv[f"reason:{r.get('reason')}"] += 1
        inv["control" if r.get("control") else "hot"] += 1
        y = outcome_y24(r)
        has_created = _parse_ts(r.get("created_at")) is not None
        rec = {
            "id": r.get("clip_id"), "game": r.get("game_id") or r.get("game") or "?",
            "game_name": r.get("game"), "broadcaster": r.get("broadcaster"),
            "cohort": reconstruct_cohort(r), "control": bool(r.get("control")),
            "reason": r.get("reason"), "y24": y, "raw": r,
            "v": {m: early_velocity(r, m) for m in EARLY_MS},
            "v15": get_milestone(r, 1.5, _early_tol(1.5)),  # prune variable (diagnostics only)
        }
        if any(rec["v"][m] is not None for m in EARLY_MS):
            all_early.append(rec)
        if y is not None and has_created:
            (control if rec["control"] else hot).append(rec)
        elif y is None:
            excluded.append(rec)
    return control, hot, all_early, excluded, dict(inv)


# ==========================================================================
# Rank / correlation primitives (hand-implemented; numpy vector math)
# ==========================================================================
def rankdata_average(a):
    a = np.asarray(a, float)
    n = len(a)
    order = a.argsort(kind="mergesort")
    ranks = np.empty(n, float)
    ranks[order] = np.arange(1, n + 1, dtype=float)
    sa = a[order]
    i = 0
    while i < n:
        j = i
        while j + 1 < n and sa[j + 1] == sa[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + 1 + j + 1) / 2.0
        i = j + 1
    return ranks


def frac_rank(vals):
    n = len(vals)
    return (rankdata_average(vals) - 0.5) / n


def _pearson(x, y):
    x = np.asarray(x, float); y = np.asarray(y, float)
    if len(x) < 2 or np.std(x) == 0 or np.std(y) == 0:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def spearman_rho(x, y):
    if len(x) < 2:
        return None
    return _pearson(rankdata_average(x), rankdata_average(y))


def kendall_tau_b(x, y):
    x = np.asarray(x, float); y = np.asarray(y, float)
    n = len(x)
    if n < 2:
        return None
    c = d = tx = ty = 0
    for i in range(n):
        for j in range(i + 1, n):
            dx = x[i] - x[j]; dy = y[i] - y[j]
            if dx == 0 and dy == 0:
                continue
            if dx == 0:
                ty += 1
            elif dy == 0:
                tx += 1
            elif (dx > 0) == (dy > 0):
                c += 1
            else:
                d += 1
    denom = math.sqrt((c + d + tx) * (c + d + ty))
    return (c - d) / denom if denom > 0 else None


def _group_pairs(pairs):
    by = defaultdict(lambda: ([], []))
    for p in pairs:
        by[p["game"]][0].append(p["x"])
        by[p["game"]][1].append(p["y"])
    return by


def pooled_within_group_spearman(pairs):
    """Rank x and y WITHIN each game (n>=2), pool ranks, Pearson => (rho, n_pairs).
    Games with a single clip can't be ranked and are dropped from the pool."""
    rx, ry = [], []
    for game, (xs, ys) in _group_pairs(pairs).items():
        if len(xs) < 2:
            continue
        rx.extend(frac_rank(xs)); ry.extend(frac_rank(ys))
    if len(rx) < 2:
        return None, len(rx)
    return _pearson(rx, ry), len(rx)


def fisher_z_meta(rho_ns):
    """SECONDARY cross-check: inverse-variance meta of per-game rhos. Returns dict with
    pooled rho, CI, and I^2 heterogeneity (drives refuse-single-number if I2>0.6)."""
    zs, ws = [], []
    for rho, n in rho_ns:
        if rho is None or n < 4 or abs(rho) >= 1:
            continue
        z = math.atanh(rho)
        w = (n - 3) / 1.06          # Bonett-Wright variance for Spearman-z
        zs.append(z); ws.append(w)
    if len(zs) < 2:
        return None
    zs = np.array(zs); ws = np.array(ws)
    zp = float((ws * zs).sum() / ws.sum())
    se = math.sqrt(1.0 / ws.sum())
    q = float((ws * (zs - zp) ** 2).sum())
    k = len(zs)
    i2 = max(0.0, (q - (k - 1)) / q) if q > 0 else 0.0
    return {"rho": math.tanh(zp), "ci": (math.tanh(zp - 1.96 * se), math.tanh(zp + 1.96 * se)),
            "i2": i2, "k": k}


def cluster_bootstrap_ci(pairs, rng, B=BOOT_B):
    """Percentile 95% CI on pooled rho, resampling COHORT clusters with replacement."""
    if len(pairs) < 10:
        return None, 0.0
    by_cluster = defaultdict(list)
    for p in pairs:
        by_cluster[p["cohort"]].append(p)
    clusters = list(by_cluster.values())
    rhos, discarded = [], 0
    for _ in range(B):
        idx = rng.integers(0, len(clusters), len(clusters))
        sample = [p for i in idx for p in clusters[i]]
        rho, _ = pooled_within_group_spearman(sample)
        if rho is None:
            discarded += 1
        else:
            rhos.append(rho)
    if len(rhos) < B * 0.5:
        return None, discarded / B
    lo, hi = np.percentile(rhos, [2.5, 97.5])
    return (float(lo), float(hi)), discarded / B


def permutation_p(pairs, rho_obs, rng, P=PERM_P):
    """Within-game shuffle of y, re-pool rho; two-sided p with +1 correction."""
    if rho_obs is None or len(pairs) < 10:
        return None
    by = _group_pairs(pairs)
    games = [(g, np.array(xs, float), np.array(ys, float))
             for g, (xs, ys) in by.items() if len(xs) >= 2]
    if not games:
        return None
    hits = 0
    for _ in range(P):
        rx, ry = [], []
        for g, xs, ys in games:
            yp = ys.copy(); rng.shuffle(yp)
            rx.extend(frac_rank(xs)); ry.extend(frac_rank(yp))
        rp = _pearson(rx, ry)
        if rp is not None and abs(rp) >= abs(rho_obs):
            hits += 1
    return (1 + hits) / (P + 1)


def bh_fdr(pvals, q=BH_Q):
    """Benjamini-Hochberg; return a boolean 'significant' per input index."""
    items = [(p, i) for i, p in enumerate(pvals) if p is not None]
    if not items:
        return [False] * len(pvals)
    items.sort()
    m = len(items)
    sig = [False] * len(pvals)
    kmax = 0
    for rank, (p, _) in enumerate(items, 1):
        if p <= q * rank / m:
            kmax = rank
    for rank, (p, i) in enumerate(items, 1):
        if rank <= kmax:
            sig[i] = True
    return sig


def leave_one_out_stability(pairs, rho_obs):
    """Flag UNSTABLE if dropping the highest-velocity clip (or max rank-residual clip)
    shifts pooled rho by > G_LOO. Needs n>=8."""
    if rho_obs is None or len(pairs) < 8:
        return None
    hi = max(range(len(pairs)), key=lambda i: pairs[i]["x"])
    drops = {hi}
    # max |rank residual| clip
    rx = frac_rank([p["x"] for p in pairs]); ry = frac_rank([p["y"] for p in pairs])
    drops.add(int(np.argmax(np.abs(rx - ry))))
    for di in drops:
        rho2, _ = pooled_within_group_spearman([p for k, p in enumerate(pairs) if k != di])
        if rho2 is not None and abs(rho2 - rho_obs) > G_LOO:
            return False
    return True


def wilson_ci(hits, n, z=1.96):
    if n == 0:
        return (None, None)
    p = hits / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (center - half, center + half)


# ==========================================================================
# Selection-actionable metrics
# ==========================================================================
def _top_tercile_flags(vals):
    """Boolean 'in top tercile' by within-group fractional rank (> 2/3)."""
    return [r > 2.0 / 3.0 for r in frac_rank(vals)]


def tercile_precision_lift(pairs):
    """Micro-averaged P(top-tercile 24h | top-tercile early) across games, Wilson CI,
    lift vs the CHANCE baseline. The chance baseline is NOT a constant 1/3: the discrete
    `r>2/3` cut flags c(n) clips per game and c(n)/n only equals 1/3 when n is a multiple
    of 3. So the correct per-game chance precision is (#outcome-flagged / n), and the
    micro-averaged baseline is sum_g(cell_g * outflag_g/n_g) / sum_g(cell_g) — the
    expected hit rate under the within-game permutation null the rest of the module uses."""
    hits = cell = 0
    exp_hits = 0.0
    for game, (xs, ys) in _group_pairs(pairs).items():
        n = len(xs)
        if n < 3:
            continue
        te = _top_tercile_flags(xs); to = _top_tercile_flags(ys)
        ne = sum(te); no = sum(to)
        cell += ne
        exp_hits += ne * (no / n)          # expected hits under independence
        for e, o in zip(te, to):
            if e and o:
                hits += 1
    if cell < MIN_CELL_SELECT:
        return None
    base = exp_hits / cell                 # data-driven chance precision (~1/3, corrected)
    prec = hits / cell
    lo, hi = wilson_ci(hits, cell)
    return {"precision": prec, "base": base, "lift": prec / base, "ci": (lo, hi),
            "lift_ci": (lo / base, hi / base), "cell": cell, "hits": hits}


def precision_at_k(pairs):
    out = {}
    ks = [1, 3, 5]
    tot = defaultdict(lambda: [0, 0, 0.0])   # k -> [hits, cell, expected_random_hits]
    frac_k_hits = frac_k_cell = 0
    frac_k_rand = 0.0
    ties1 = 0
    for game, (xs, ys) in _group_pairs(pairs).items():
        n = len(xs)
        if n < 2:
            continue
        ox = np.argsort(-np.asarray(xs, float), kind="mergesort")
        oy = set(np.argsort(-np.asarray(ys, float), kind="mergesort")[:max(1, n // 5)])
        # tie awareness for k=1
        xarr = np.asarray(xs, float)
        if np.sum(xarr == xarr.max()) > 1:
            ties1 += 1
        for k in ks:
            if n < k:
                continue
            topx = set(ox[:k])
            topy = set(np.argsort(-np.asarray(ys, float), kind="mergesort")[:k])
            tot[k][0] += len(topx & topy)
            tot[k][1] += k
            tot[k][2] += k * (k / n)          # expected overlap under independence
        kk = max(1, n // 5)
        topx20 = set(ox[:kk])
        frac_k_hits += len(topx20 & oy); frac_k_cell += kk
        frac_k_rand += kk * (kk / n)
    for k in ks:
        h, c, r = tot[k]
        if c:
            out[f"k{k}"] = {"precision": h / c, "random": r / c, "cell": c}
    if frac_k_cell:
        out["top20pct"] = {"precision": frac_k_hits / frac_k_cell,
                           "random": frac_k_rand / frac_k_cell, "cell": frac_k_cell}
    out["ties_at_k1_games"] = ties1
    return out


def captured_value_ratio(pairs, rng, B=1000):
    """(v_sel - v_rand)/(v* - v_rand) for the early-#1 pick, median across games + CI."""
    ratios = []
    for game, (xs, ys) in _group_pairs(pairs).items():
        if len(xs) < 3:
            continue
        ys = np.asarray(ys, float); xs = np.asarray(xs, float)
        vstar = ys.max(); vrand = ys.mean(); vsel = ys[int(np.argmax(xs))]
        if vstar == vrand:
            continue
        ratios.append((vsel - vrand) / (vstar - vrand))
    if len(ratios) < 3:
        return None
    med = float(np.median(ratios))
    boots = [float(np.median(rng.choice(ratios, len(ratios), replace=True))) for _ in range(B)]
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return {"median": med, "ci": (float(lo), float(hi)), "n_games": len(ratios)}


def _quartile_within(vals):
    """Assign each clip a quartile 0..3 by EXACT within-game rank (not Hazen*4, which
    quantizes badly for small n). floor((rank-1)*4/n) is balanced when n divides 4 and
    near-balanced otherwise."""
    r = rankdata_average(vals)
    n = len(vals)
    return [min(3, int((ri - 1) * 4 / n)) for ri in r]


def quartile_transition_matrix(pairs):
    """4x4 P(24h quartile | early quartile), pooled over games with n>=8 so a tiny game
    can't dump its lone top clip into a fixed quartile and manufacture false persistence."""
    counts = np.zeros((4, 4))
    used = 0
    for game, (xs, ys) in _group_pairs(pairs).items():
        if len(xs) < 8:                    # need enough clips for a real 4-way split
            continue
        qx = _quartile_within(xs); qy = _quartile_within(ys)
        for a, b in zip(qx, qy):
            counts[a][b] += 1
        used += len(xs)
    if used < 8:
        return None
    row = counts.sum(axis=1, keepdims=True)
    probs = np.divide(counts, row, out=np.zeros_like(counts), where=row > 0)
    return {"probs": probs.tolist(), "counts": counts.tolist(),
            "row_counts": [int(x) for x in counts.sum(axis=1)],
            "winner_stays": float(probs[3][3]), "dark_horse": float(probs[0][3]),
            "n": used}


# ==========================================================================
# Survivorship diagnostics
# ==========================================================================
def survivorship_diagnostics(control, hot, all_early):
    out = {}
    for m in EARLY_MS:
        cp = [{"game": r["game"], "cohort": r["cohort"], "x": r["v"][m], "y": r["y24"]}
              for r in control if r["v"][m] is not None and r["y24"] is not None]
        hp = [{"game": r["game"], "cohort": r["cohort"], "x": r["v"][m], "y": r["y24"]}
              for r in hot if r["v"][m] is not None and r["y24"] is not None]
        rc, nc = pooled_within_group_spearman(cp)
        rh, nh = pooled_within_group_spearman(hp)
        out[m] = {"control_rho": rc, "control_n": nc, "hot_rho": rh, "hot_n": nh,
                  "attenuation": (rc - rh) if (rc is not None and rh is not None) else None}
    # prune rate over non-control clips that had an early snapshot
    reg = [r for r in all_early if not r["control"]]
    pruned = sum(1 for r in reg if r["reason"] == "pruned")
    out["prune_rate"] = pruned / len(reg) if reg else None
    # range restriction: control's early-velocity spread vs the FULL registered pool.
    # <1 means control spans a narrower early range than everything registered, so its
    # correlation understates the full-population signal (a conservative lower bound).
    def sd_log(pop):
        vals = [math.log1p(r["v"][1.0]) for r in pop if r["v"].get(1.0) is not None]
        return float(np.std(vals)) if len(vals) >= 2 else None
    sc, sall = sd_log(control), sd_log(all_early)
    out["range_restriction_ratio"] = (sc / sall) if (sc and sall) else None
    return out


# ==========================================================================
# Early development (answerable NOW — needs no 24h completion)
# ==========================================================================
EARLY_PAIRS = [(0.5, 1.5), (0.5, 1.0), (1.0, 1.5)]


def _milestone_views(rec, target_h):
    m = get_milestone(rec, target_h, _early_tol(target_h))
    return m[0] if m else None


def early_development(clips, rng):
    """Does the EARLY view leaderboard predict the slightly-later one? Uses every clip
    that has BOTH milestones (pruned clips included — pruning happens AT 1.5h, after both
    snapshots exist), so this is answerable long before any 24h completion. Two reads:
    control = clean (mid-ranked, not discovery-selected on early views); all = bigger n
    but includes hot clips picked FOR high early views (range-restricted) → descriptive.
    View counts are CUMULATIVE (later >= earlier) so some persistence is mechanical; the
    rank correlation shows how much the ORDER reshuffles between the two checkpoints."""
    out = {}
    for me, ml in EARLY_PAIRS:
        out[f"{me}->{ml}"] = {}
        for pop, keep in (("control", True), ("all", False)):
            pairs = []
            for r in clips:
                if keep and not r.get("control"):
                    continue
                ve = _milestone_views(r, me); vl = _milestone_views(r, ml)
                if ve is not None and vl is not None:
                    pairs.append({"game": r.get("game_id") or r.get("game") or "?",
                                  "cohort": reconstruct_cohort(r), "x": ve, "y": vl})
            rho, n = pooled_within_group_spearman(pairs)
            ci = None
            if rho is not None and n >= 10:
                ci, _ = cluster_bootstrap_ci(pairs, rng)
            out[f"{me}->{ml}"][pop] = {"rho": rho, "n": n, "ci": ci,
                                       "lift": tercile_precision_lift(pairs)}
    return out


# ==========================================================================
# Headline decision
# ==========================================================================
def decide_earliest_usable(results):
    """Earliest milestone clearing the USABLE gate; else a NOT-YET verdict."""
    best = None
    for m in EARLY_MS:
        r = results.get(m)
        if not r:
            continue
        rho, ci = r.get("rho"), r.get("rho_ci")
        lift_info = r.get("lift")
        n = r.get("n_pairs") or 0
        stable = r.get("stable")
        if rho is None or ci is None or lift_info is None:
            continue
        lift, lift_ci = lift_info["lift"], lift_info["lift_ci"]
        usable = (rho >= G_RHO and ci[0] > 0 and lift >= G_LIFT and lift_ci[0] > 1.0
                  and n >= MIN_PAIRS_RHO and stable is True)
        cand = {"m": m, "rho": rho, "ci": ci, "lift": lift, "lift_ci": lift_ci,
                "n": n, "stable": stable, "usable": usable}
        if usable:
            return cand                      # earliest usable wins
        if best is None:
            best = cand
    return best or {"usable": False, "m": None}


# ==========================================================================
# Rendering
# ==========================================================================
def _fmt_ci(ci):
    return f"[{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else "n/a"


def _fmt(x, nd=2):
    return "n/a" if x is None else f"{x:.{nd}f}"


def _spans(ci, base):
    """True if the CI includes its chance baseline (rho→0, lift→1.0, captured→0) — i.e.
    the result is NOT yet distinguishable from chance. A missing CI counts as spanning."""
    return ci is None or (ci[0] <= base <= ci[1])


def _strength(rho):
    a = abs(rho)
    return "strong" if a >= 0.7 else "moderate" if a >= 0.4 else "weak"


def _render_early_development(W, ed, active_n=0):
    if not ed:
        return
    head = ed.get("0.5->1.5", {})
    c = head.get("control", {}); a = head.get("all", {})
    # headline read: prefer the clean control sample once it has enough pairs, else all
    prim, plabel = ((c, "control sample") if (c.get("rho") is not None and c.get("n", 0) >= 20)
                    else (a, "all-clips sample"))
    W("## Early development — does the 30-min view leaderboard hold at 1.5h?\n")
    if active_n:
        W(f"_Includes {active_n} in-flight clips (still being tracked) alongside the "
          f"archived ones, so this reflects the full population at these checkpoints._\n")
    if prim.get("rho") is None:
        W("Not enough clips have both a 30-min and a 1.5h snapshot yet — check back shortly.\n")
        return
    lift = prim.get("lift")
    liftstr = ""
    if lift:
        liftstr = (f" Of clips in the **top third by 30-min views**, "
                   f"**{lift['precision']:.0%}** were still top third at 1.5h "
                   f"({lift['lift']:.2f}× chance).")
    holds = ("largely holds" if prim["rho"] >= 0.7 else
             "partly holds" if prim["rho"] >= 0.4 else "reshuffles a lot")
    W(f"**ρ = {prim['rho']:+.2f}** (Spearman, within-game, {plabel}, n={prim['n']}, "
      f"95% CI {_fmt_ci(prim.get('ci'))}) — a **{_strength(prim['rho'])}** rank correlation, "
      f"so the 30-min order **{holds}** by 1.5h.{liftstr}")
    W("\n_View counts are cumulative (1.5h views ≥ 30-min views by definition), so some "
      "persistence is mechanical — ρ measures how much the RANKING reshuffles between the "
      "two checkpoints, which is the real question._\n")
    W("| Interval | control ρ (n) | all-clips ρ (n) |")
    W("|---|---|---|")
    for me, ml in EARLY_PAIRS:
        r = ed.get(f"{me}->{ml}", {})
        cc = r.get("control", {}); aa = r.get("all", {})
        W(f"| {me}h→{ml}h | {_fmt(cc.get('rho'))} ({cc.get('n', 0)}) | "
          f"{_fmt(aa.get('rho'))} ({aa.get('n', 0)}) |")
    W("\n_control = mid-ranked clips (a clean read); all-clips also includes clips "
      "discovered FOR their high early views (range-restricted), so treat that column as "
      "descriptive, not a clean estimate._\n")


def render_readout(ctx):
    L = []
    W = L.append
    s = ctx["stats"]
    W("# Clipkroniek — early-velocity → 24h-outcome analysis\n")
    W(f"_Generated {ctx['run_utc']} · seed {SEED}_\n")
    W(f"- Day-files: **{s['files']}** ({s['date_range'][0]} → {s['date_range'][1]}), "
      f"raw lines {s['raw_lines']}, malformed {s['malformed']}, "
      f"duplicates removed {ctx['dupes']}")
    W(f"- Usable (any clip with a valid 24h outcome): **{ctx['usable']}** · "
      f"control-with-24h: **{ctx['control24']}**\n")

    _render_early_development(W, ctx.get("early_dev"), ctx.get("active_n", 0))

    if not ctx["gate_passed"]:
        W(f"## ⏳ Not enough data yet\n")
        W(f"Usable clips = {ctx['usable']} (need ≥{FLOOR_USABLE}), "
          f"control-with-24h = {ctx['control24']} (need ≥{FLOOR_CONTROL24}). "
          f"The tracker keeps banking trajectories every 30 min — come back after more days.")
        return "\n".join(L)

    # HEADLINE
    h = ctx["headline"]
    W("## How early can we tell a winner?\n")
    if h.get("usable"):
        cv = ctx["results"][h["m"]].get("captured")
        cvs = (f" and the single fastest-starter captures ~{cv['median']*100:.0f}% of the "
               f"achievable 24h payoff over a random pick"
               if (cv and cv["ci"][0] > 0) else "")
        W(f"**Yes — by {int(h['m']*60)} min.** On the unbiased control sample, "
          f"velocity at {h['m']}h ranks with the 24h outcome at ρ={h['rho']:.2f} "
          f"(95% CI {_fmt_ci(h['ci'])}); top-tercile early clips reach the 24h top tercile "
          f"{h['lift']:.2f}× the chance base rate{cvs}. This clears the usable bar "
          f"(ρ≥{G_RHO}, lift≥{G_LIFT}, CI excludes chance, n={h['n']}, stable).")
    else:
        m = h.get("m")
        if m is None:
            W("**Not yet.** No early milestone has enough clean pairs to estimate the signal. "
              "Keep collecting.")
        else:
            r = ctx["results"][m]
            tail = ("Signal is directional but not yet at the usable bar — re-run after more days."
                    if not _spans(r.get("rho_ci"), 0.0) else
                    "Not yet distinguishable from chance (the CI still spans 0) — keep collecting.")
            W(f"**Not yet — best so far is {m}h** at ρ={_fmt(r.get('rho'))} "
              f"(CI {_fmt_ci(r.get('rho_ci'))}, n={r.get('n_pairs')}). "
              f"It has not cleared the usable bar (need ρ≥{G_RHO} with CI>0, "
              f"lift≥{G_LIFT} with CI>1, n≥{MIN_PAIRS_RHO}, stable). " + tail)
    W("")

    # PRIMARY PREDICTIVE READOUT
    W("## Primary predictive readout (control only — the unbiased sample)\n")
    W("| Milestone | ρ (Spearman) | 95% CI | n | τb | perm p (BH) | stable | verdict |")
    W("|---|---|---|---|---|---|---|---|")
    for m in EARLY_MS:
        r = ctx["results"].get(m, {})
        if r.get("rho") is None:
            verdict = "insufficient"
        elif _spans(r.get("rho_ci"), 0.0):
            verdict = "chance"
        elif ctx["headline"].get("usable") and ctx["headline"]["m"] == m:
            verdict = "actionable"
        else:
            verdict = "preliminary"
        W(f"| {m}h | {_fmt(r.get('rho'))} | {_fmt_ci(r.get('rho_ci'))} | "
          f"{r.get('n_pairs','–')} | {_fmt(r.get('tau_b'))} | "
          f"{_fmt(r.get('perm_p'),3)}{'*' if r.get('sig') else ''} | "
          f"{'ok' if r.get('stable') else ('UNSTABLE' if r.get('stable') is False else '–')} | {verdict} |")
    fz = ctx.get("fisher")
    if fz:
        note = " — I²>0.6, prefer the per-game table over one number" if fz["i2"] > 0.6 else ""
        W(f"\n_Secondary Fisher-z meta ({fz['k']} games): ρ={fz['rho']:.2f} "
          f"{_fmt_ci(fz['ci'])}, I²={fz['i2']:.0%}{note}._")
    W("")

    # SELECTION
    W("## Selection-actionable (control)\n")
    for m in EARLY_MS:
        r = ctx["results"].get(m, {})
        lift = r.get("lift"); cap = r.get("captured")
        if lift:
            caps = (f"; captured-value {cap['median']:.0%} {_fmt_ci(cap['ci'])}"
                    if (cap and cap["ci"][0] > 0) else "")
            marker = " — not yet distinguishable from chance" if _spans(lift["lift_ci"], 1.0) else ""
            W(f"- **{m}h** top-tercile precision {lift['precision']:.0%} "
              f"(Wilson {lift['ci'][0]:.0%}–{lift['ci'][1]:.0%}, n={lift['cell']}), "
              f"lift {lift['lift']:.2f}× vs chance base{caps}{marker}")
        else:
            W(f"- **{m}h** selection test: insufficient (need top-early cell ≥{MIN_CELL_SELECT}).")
    W("")

    # TRANSITION MATRIX
    tm = ctx.get("transition")
    if tm:
        W("## Quartile transition — P(24h quartile | early-1h quartile), control\n")
        W("| early ↓ / 24h → | Q1 | Q2 | Q3 | Q4 | row n |")
        W("|---|---|---|---|---|---|")
        for i, row in enumerate(tm["probs"], 1):
            rc = tm["row_counts"][i - 1]
            W(f"| Q{i} | " + " | ".join(f"{p:.0%}" for p in row) + f" | {rc} |")
        W(f"\n_Winner-stays P(Q4|Q4)={tm['winner_stays']:.0%}; "
          f"dark-horse P(Q4|Q1)={tm['dark_horse']:.0%}; n={tm['n']} "
          f"(games with ≥8 control clips only)._\n")

    # PER-GAME
    pg = ctx.get("per_game")
    if pg:
        W("## Per-game (control, n≥12)\n")
        W("| Game | ρ | 95% CI | n | τb |")
        W("|---|---|---|---|---|")
        for g in pg:
            W(f"| {g['name']} | {_fmt(g['rho'])} | {_fmt_ci(g['ci'])} | {g['n']} | {_fmt(g['tau_b'])} |")
        W("\n_Per-game rhos are one of many comparisons (BH-FDR q=0.10 applied); "
          "don't headline a single game the pooled result didn't support._\n")

    # SURVIVORSHIP
    sd = ctx.get("survivor")
    if sd:
        W("## Survivorship diagnostics — ⚠️ HOT set is biased, do NOT use it for selection\n")
        W("| Milestone | control ρ (n) | hot ρ (n) | attenuation |")
        W("|---|---|---|---|")
        for m in EARLY_MS:
            d = sd[m]
            W(f"| {m}h | {_fmt(d['control_rho'])} ({d['control_n']}) | "
              f"{_fmt(d['hot_rho'])} ({d['hot_n']}) | {_fmt(d['attenuation'])} |")
        W(f"\n_Prune rate (non-control clips dropped at 1.5h): {_fmt(sd.get('prune_rate'),2)}; "
          f"range-restriction ratio (SD of log early velocity, control ÷ all-registered): "
          f"{_fmt(sd.get('range_restriction_ratio'))} "
          f"(a value <1 means control spans a narrower early range than the full pool, so its "
          f"ρ understates the full-population signal — a conservative lower bound)._\n")

    # RECOMMENDATION
    W("## Operating recommendation\n")
    if h.get("usable"):
        cv = ctx["results"][h["m"]].get("captured")
        pct = f"~{cv['median']*100:.0f}%" if (cv and cv["ci"][0] > 0) else "a meaningful share of"
        W(f"Rank the live clip pool by **{h['m']}h (={int(h['m']*60)}min) velocity within game**; "
          f"expect to capture {pct} of best-case 24h views by picking the fastest starter. "
          f"Do **not** trust picks earlier than {h['m']}h.")
    else:
        W("Keep collecting — the early signal is not yet strong or certain enough to drive "
          "selection. Re-run after more days; this readout refreshes weekly.")
    return "\n".join(L)


def build_sidecar(ctx):
    out = {"generated_utc": ctx["run_utc"], "seed": SEED, "gate_passed": ctx["gate_passed"],
           "usable": ctx["usable"], "control24": ctx["control24"],
           "headline": {k: v for k, v in ctx["headline"].items() if k != "raw"},
           "milestones": {}}
    for m in EARLY_MS:
        r = ctx["results"].get(m, {})
        out["milestones"][str(m)] = {
            "rho": r.get("rho"), "rho_ci": r.get("rho_ci"), "n_pairs": r.get("n_pairs"),
            "perm_p": r.get("perm_p"), "significant": r.get("sig"), "stable": r.get("stable"),
            "tercile_lift": (r.get("lift") or {}).get("lift"),
            "captured_value": (r.get("captured") or {}).get("median"),
        }
    return out


def write_outputs(client, bucket, text, sidecar):
    print(text)
    if client is None:
        print("\n[warn] R2 not configured — readout printed to stdout only.", file=sys.stderr)
        return
    try:
        client.put_object(Bucket=bucket, Key=READOUT_KEY,
                          Body=text.encode("utf-8"), ContentType="text/markdown")
        client.put_object(Bucket=bucket, Key=SIDECAR_KEY,
                          Body=json.dumps(sidecar, ensure_ascii=False, indent=2).encode("utf-8"),
                          ContentType="application/json")
        base = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
        print(f"\n[ok] readout → {base}/{READOUT_KEY}" if base else f"\n[ok] readout → {READOUT_KEY}",
              file=sys.stderr)
    except Exception as e:
        print(f"\n[error] R2 write failed: {e}", file=sys.stderr)
        sys.exit(1)


# ==========================================================================
# Orchestration
# ==========================================================================
def analyze(clips, active, run_utc, stats, dupes):
    control, hot, all_early, excluded, inv = partition(clips)   # 24h analysis: archived only
    usable = len(control) + len(hot)
    control24 = len(control)
    rng = np.random.default_rng(SEED)

    # early development can use in-flight clips too (they have the early snapshots)
    early_pool, _ = dedupe_clips(clips + active)
    ctx = {"run_utc": run_utc, "inv": inv, "usable": usable, "control24": control24,
           "results": {}, "gate_passed": False, "headline": {"usable": False, "m": None},
           "stats": stats, "dupes": dupes, "active_n": len(active),
           "early_dev": early_development(early_pool, rng)}

    if usable < FLOOR_USABLE or control24 < FLOOR_CONTROL24:
        return ctx

    ctx["gate_passed"] = True

    perm_ps = []
    perm_index = {}
    for m in EARLY_MS:
        pairs = [{"game": r["game"], "cohort": r["cohort"], "broadcaster": r["broadcaster"],
                  "x": r["v"][m], "y": r["y24"]}
                 for r in control if r["v"][m] is not None and r["y24"] is not None]
        rho, n = pooled_within_group_spearman(pairs)
        res = {"n_pairs": n, "rho": rho}
        if rho is not None and n >= 10:
            res["rho_ci"], res["boot_discard"] = cluster_bootstrap_ci(pairs, rng)
            res["tau_b"] = kendall_tau_b([p["x"] for p in pairs], [p["y"] for p in pairs]) if n < 20 else None
            res["stable"] = leave_one_out_stability(pairs, rho)
            p = permutation_p(pairs, rho, rng)
            res["perm_p"] = p
            perm_index[m] = len(perm_ps)
            perm_ps.append(p)
        res["lift"] = tercile_precision_lift(pairs)
        res["patk"] = precision_at_k(pairs)
        res["captured"] = captured_value_ratio(pairs, rng)
        ctx["results"][m] = res

    # BH-FDR across the milestone permutation family
    sig = bh_fdr(perm_ps)
    for m, idx in perm_index.items():
        ctx["results"][m]["sig"] = sig[idx]

    # 1h transition matrix + per-game + fisher + survivorship
    pairs_1h = [{"game": r["game"], "cohort": r["cohort"], "x": r["v"][1.0], "y": r["y24"]}
                for r in control if r["v"].get(1.0) is not None and r["y24"] is not None]
    ctx["transition"] = quartile_transition_matrix(pairs_1h)

    per_game, rho_ns = [], []
    by_game = defaultdict(list)
    for r in control:
        if r["v"].get(1.0) is not None and r["y24"] is not None:
            by_game[r["game"]].append(r)
    for game, rs in by_game.items():
        xs = [r["v"][1.0] for r in rs]; ys = [r["y24"] for r in rs]
        rho = spearman_rho(xs, ys)
        rho_ns.append((rho, len(rs)))
        if len(rs) >= 12 and rho is not None:
            pr = [{"game": game, "cohort": r["cohort"], "x": r["v"][1.0], "y": r["y24"]} for r in rs]
            ci, _ = cluster_bootstrap_ci(pr, rng)
            per_game.append({"name": rs[0]["game_name"] or game, "rho": rho, "ci": ci,
                             "n": len(rs), "tau_b": kendall_tau_b(xs, ys) if len(rs) < 20 else None})
    ctx["per_game"] = sorted(per_game, key=lambda g: (g["rho"] is None, -(g["rho"] or 0)))
    ctx["fisher"] = fisher_z_meta(rho_ns)
    ctx["survivor"] = survivorship_diagnostics(control, hot, all_early)

    ctx["headline"] = decide_earliest_usable(ctx["results"])
    return ctx


def main():
    try:                              # markdown readout uses →/⚠️ etc.; never let a
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # locale crash lose it
    except Exception:
        pass
    have_r2 = r2_configured()
    if not have_r2:
        sys.exit("tracker_analyze needs R2 (the dataset lives there).")
    client = _r2_client()
    bucket = env("R2_BUCKET")
    run_utc = now_utc().isoformat(timespec="seconds")

    records, stats = load_dataset(client, bucket)
    clips, dupes = dedupe_clips(records)
    active = load_active(client, bucket)
    ctx = analyze(clips, active, run_utc, stats, dupes)
    text = render_readout(ctx)
    sidecar = build_sidecar(ctx)
    write_outputs(client, bucket, text, sidecar)


if __name__ == "__main__":
    main()
