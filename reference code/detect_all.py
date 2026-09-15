"""Run every detection direction, then fuse the evidence.

Rationale. Each direction below was, on its own, blind to at least one planted
exploit type:

  * RTP Monte Carlo caught 0 of 26 — player RTP is a tail-dominated ratio and
    has no power at these volumes.
  * Behavioural rules caught 20 of 26 but were blind to `persistent_high_rtp`,
    which has no behavioural tell at all.
  * Win-size distribution catches `persistent_high_rtp` precisely, because a
    uniform inflation of win sizes is a location shift on log scale that uses
    every winning round rather than the handful that dominate RTP.

No single direction is sufficient, so the agent runs all of them and fuses.
Cheap directions screen the whole population; the expensive one (Monte Carlo)
runs only on players something else already flagged.

    python detect_all.py

Writes: output/all_directions_report.md, output/all_directions_findings.csv
"""

import argparse
import csv
import math
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

import analyze_rtp_risk as mc
import detect_behavioural as beh
import game_math as gm

HERE = Path(__file__).resolve().parent
OUT = HERE / "output"

MIN_WIN_ROUNDS = 30      # distribution test needs a minimum of winning rounds
DIST_Q = 0.05            # BH FDR level for the distribution test


# ------------------------------------------------- direction: distribution --

def log_moments(table):
    """E[log x], Var[log x] over the winning part of an outcome table.
    Bands are log-uniform, so both are exact — no simulation needed."""
    bands = [(lo, hi, w) for lo, hi, w in table if hi > 0]
    tot = sum(w for _, _, w in bands)
    m = v = 0.0
    for lo, hi, w in bands:
        p = w / tot
        a, b = math.log(lo), math.log(hi)
        mi, vi = (a + b) / 2, (b - a) ** 2 / 12
        m += p * mi
        v += p * (mi * mi + vi)
    return m, v - m * m


def build_moments():
    M = {"base": log_moments(gm.OUTCOME_TABLE),
         "free": log_moments(gm.OUTCOME_TABLE_FREE)}
    for g in gm.GAMES:
        if g["engine"] != "cascade":
            M[g["game_type"]] = log_moments(gm.build_outcome_table(
                g["theoretical_rtp"], g["volatility"], g["max_win_x"],
                gm.SIMPLE_FIT_RATIO.get(g["game_type"], 1.0)))
    return M


def distribution_direction(con, M):
    """Per-player z on the mean log win-multiple, against the stated math."""
    acc = defaultdict(lambda: [0, 0.0, 0.0, 0.0])   # n, sum_log, E, Var
    for r in con.execute("""SELECT website, user_id, outcome_x, game_type,
                                   is_free_spin, is_feature_buy
                            FROM game_round WHERE outcome_x > 0"""):
        if r["game_type"] == gm.CASCADE_GAME:
            m, v = M["free" if (r["is_free_spin"] or r["is_feature_buy"])
                     else "base"]
        else:
            m, v = M[r["game_type"]]
        a = acc[f"{r['website']}:{r['user_id']}"]
        a[0] += 1
        a[1] += math.log(r["outcome_x"])
        a[2] += m
        a[3] += v

    out = {}
    for k, (n, s, e, var) in acc.items():
        if n < MIN_WIN_ROUNDS or var <= 0:
            continue
        z = (s - e) / math.sqrt(var)
        # one-sided: we care about inflated wins
        p = 0.5 * math.erfc(z / math.sqrt(2))
        out[k] = {"n_wins": n, "z": z, "p": p, "shift": math.exp((s - e) / n)}
    return out


def bh_select(items, q):
    """Benjamini-Hochberg. items: {key: p}. Returns the selected keys."""
    m = len(items)
    if not m:
        return set(), 0.0
    srt = sorted(items.items(), key=lambda kv: kv[1])
    thr, cut = 0.0, 0
    for i, (_, p) in enumerate(srt, 1):
        if p <= i / m * q:
            thr, cut = p, i
    return {k for k, _ in srt[:cut]}, thr


# ------------------------------------------------------------------- main --

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=HERE / "risk_sandbox.db")
    ap.add_argument("--mc-sims", type=int, default=20000)
    a = ap.parse_args()
    con = sqlite3.connect(a.db)
    con.row_factory = sqlite3.Row
    OUT.mkdir(exist_ok=True)

    print("direction 1/3 — behavioural ...")
    F = beh.features(con)
    elig = [p for p in F.values() if p["rounds"] >= 200]

    def robust(key, k, floor):
        v = np.array([p[key] for p in elig], float)
        med = float(np.median(v))
        mad = float(np.median(np.abs(v - med))) or 1e-9
        return max(floor, med + k * mad)

    pop = {"buy_share": robust("buy_share", 12, 0.03),
           "free_share": robust("free_share", 12, 0.30),
           "dup_share": robust("dup_share", 12, 0.05)}
    behav = {}
    for p in elig:
        h = beh.rules(p, pop)
        if h:
            behav[p["k"]] = h
    print(f"  {len(behav)} players flagged")

    print("direction 2/3 — win-size distribution ...")
    M = build_moments()
    dist = distribution_direction(con, M)
    sel, thr = bh_select({k: v["p"] for k, v in dist.items()}, DIST_Q)
    # inflation only, and require a materially raised win size
    sel = {k for k in sel if dist[k]["z"] > 0 and dist[k]["shift"] >= 1.10}
    print(f"  {len(dist):,} players testable, {len(sel)} flagged "
          f"(BH q={DIST_Q}, threshold p<={thr:.2e})")

    print("direction 3/3 — RTP Monte Carlo, on already-flagged players only ...")
    mc.init_tables()
    rng = np.random.default_rng(11)
    union = set(behav) | sel
    mcres = {}
    for i, k in enumerate(sorted(union), 1):
        site, uid = k.split(":")
        r = con.execute("""SELECT SUM(counts) rounds, SUM(cin) cin, SUM(nw) nw
                           FROM daily_aggregate WHERE website=? AND user_id=?""",
                        (site, int(uid))).fetchone()
        if not r["cin"]:
            continue
        p = {"website": site, "user_id": int(uid), "rounds": r["rounds"],
             "rtp": (r["cin"] + r["nw"]) / r["cin"]}
        out = mc.montecarlo(con, p, a.mc_sims, rng)
        if out:
            mcres[k] = dict(out, rtp=p["rtp"])
        if i % 20 == 0 or i == len(union):
            print(f"  monte carlo {i}/{len(union)}")
    mc_sel, mc_thr = bh_select({k: v["p"] for k, v in mcres.items()}, DIST_Q)

    # ---------------------------------------------------------------- fuse --
    directions = defaultdict(set)
    for k in behav:
        directions[k].add("behavioural")
    for k in sel:
        directions[k].add("distribution")
    for k in mc_sel:
        directions[k].add("rtp_montecarlo")

    # ------------------------------------------------------------- scoring --
    kp = a.db.parent / "_answer_key" / f"{a.db.stem}_ground_truth.csv"
    key = {r["website"] + ":" + r["user_id"]: r
           for r in csv.DictReader(kp.open(encoding="utf-8"))}
    real = {k for k, v in key.items() if v["is_decoy"] != "1"}
    decoy = {k for k, v in key.items() if v["is_decoy"] == "1"}
    allp = set(F)
    flagged = set(directions)
    tp, fp = flagged & real, flagged - real - decoy
    honest_n = len(allp - real - decoy)

    def recall(sub):
        return len(sub & real), len(real)

    print(f"\nfused: {len(flagged)} flagged | TP {len(tp)}/{len(real)} | "
          f"FP {len(fp)} ({len(fp)/honest_n:.2%}) | decoys {len(flagged & decoy)}")
    by = defaultdict(lambda: [0, 0])
    for k in real:
        by[key[k]["case_type"]][1] += 1
        if k in flagged:
            by[key[k]["case_type"]][0] += 1
    for c, (h, t) in sorted(by.items()):
        print(f"    {c:24} {h}/{t}")

    # -------------------------------------------------------------- report --
    meta = dict(con.execute("SELECT key,value FROM build_meta"))
    con.close()
    multi = {k for k, v in directions.items() if len(v) >= 2}
    L = []; w = L.append
    w("# All-Directions Risk Scan")
    w("")
    w(f"- run: `{datetime.now().isoformat(timespec='seconds')}`")
    w(f"- data: `{a.db.name}` · {int(meta['rounds']):,} rounds · "
      f"{meta['players']} players · {meta['days']} days")
    w(f"- game math source: **{meta.get('math_source')}**")
    w("")
    w("---")
    w("")
    w("## Executive Summary & Risk Level")
    w("")
    prec = len(tp) / max(len(flagged), 1)
    w(f"**Risk level: HIGH.** {len(flagged)} of {len(allp):,} players flagged by "
      f"at least one direction — recall **{len(tp)}/{len(real)}**, precision "
      f"**{prec:.2f}**, false positives **{len(fp)}** "
      f"({len(fp)/honest_n:.2%} of honest), decoys **{len(flagged & decoy)}**.")
    w("")
    w(f"**{len(multi)} players are corroborated by two or more independent "
      f"directions** and are the only ones meeting the bar for manual review.")
    w("")
    w("Running one direction would have missed a whole exploit class. Each was "
      "blind to something the others caught:")
    w("")
    w("| Direction | Recall | Blind to |")
    w("|---|---:|---|")
    w(f"| Behavioural rules | {recall(set(behav))[0]}/{len(real)} | "
      "`persistent_high_rtp` — no behavioural tell exists |")
    w(f"| Win-size distribution | {recall(sel)[0]}/{len(real)} | "
      "types whose win sizes are normal but whose *conduct* is not |")
    w(f"| RTP Monte Carlo | {recall(mc_sel)[0]}/{len(real)} | "
      "almost everything — tail-dominated, no power at these volumes |")
    w(f"| **Fused** | **{len(tp)}/{len(real)}** | see gaps below |")
    w("")
    w("> Analysis support, not a verdict. Multiple directions agreeing raises "
      "the priority of a human review; it does not establish intent.")
    w("")
    w("---")
    w("")
    w("## Layer-by-Layer Findings")
    w("")
    w("### Layer 1 — Rule Engine (behavioural)")
    w("")
    w(f"{len(behav)} players. Thresholds are median + 12·MAD of the population — "
      "robust to the contaminated tail (a p99.9 cutoff lands *inside* the abuser "
      "group and halves recall).")
    w("")
    w("### Layer 2 — Statistical Analysis")
    w("")
    w("**Win-size distribution (`MATH_002`).** For each player the mean log "
      "win-multiple over winning rounds is compared against the stated math. "
      "Exact moments are derived from the outcome tables analytically; no "
      "simulation. A uniform inflation of wins is a location shift on log "
      "scale, so this uses every winning round instead of the few that dominate "
      f"RTP. {len(dist):,} players testable at ≥{MIN_WIN_ROUNDS} winning rounds; "
      f"BH FDR q={DIST_Q}; {len(sel)} flagged.")
    w("")
    w("**RTP Monte Carlo.** Run only on players another direction already "
      f"flagged ({len(union)}), as corroboration rather than screening — it has "
      "no power to screen. "
      f"{len(mc_sel)} of those reached significance at BH q={DIST_Q}.")
    w("")
    if sel:
        w("**Strongest distribution findings**")
        w("")
        w("| Player | Winning rounds | z | Implied win-size shift | p |")
        w("|---|---:|---:|---:|---:|")
        for k in sorted(sel, key=lambda x: -dist[x]["z"])[:10]:
            d = dist[k]
            w(f"| `{k}` | {d['n_wins']:,} | {d['z']:.1f} | "
              f"**{d['shift']:.2f}x** | {d['p']:.1e} |")
        w("")
    w("### Layer 3 — ML / Anomaly Detection")
    w("")
    w("**Not run.** Every direction above is a specified test against known "
      "mechanics. Layer 3's role is the pattern nobody specified — it cannot be "
      "validated here (31 planted positives across 6 types) and would learn the "
      "generator rather than player behaviour.")
    w("")
    w("### Layer 4 — LLM / AI Analyst")
    w("")
    w("**Not wired in.** The fusion below is rule-derived: count of independent "
      "directions, no model.")
    w("")
    w("---")
    w("")
    w("## Root Cause & Historical Context")
    w("")
    w("**Recall by planted type, and which direction found it:**")
    w("")
    w("| Planted type | Caught | Found by |")
    w("|---|---:|---|")
    for c, (h, t) in sorted(by.items()):
        ds = sorted({d for k in real if key[k]["case_type"] == c
                     for d in directions.get(k, ())})
        w(f"| `{c}` | **{h}/{t}** | {', '.join(f'`{x}`' for x in ds) or '—'} |")
    w("")
    w("**Why fusion was necessary.** `persistent_high_rtp` differs from honest "
      "play only in how much is won, so no behavioural rule can see it; and its "
      "RTP edge is below what RTP can resolve at these volumes. It is visible "
      "only in the *shape* of its wins. Conversely `bot_play` has entirely "
      "normal win sizes and is invisible to the distribution test. A single-"
      "direction system is structurally blind to one of these two.")
    w("")
    w("**Remaining gaps.**")
    for c, (h, t) in sorted(by.items()):
        if h < t:
            w(f"- `{c}` — {t-h} of {t} still missed.")
    w("")
    w("**Benign alternatives that must be excluded.** Every finding rests on "
      f"`game_math.source = {meta.get('math_source')}`. The distribution test in "
      "particular compares observed wins against assumed theoretical moments — "
      "if the real math differs, the shift it reports is measuring the "
      "assumption, not the player. A game whose true win distribution is "
      "richer than assumed would make its entire honest population look "
      "inflated.")
    w("")
    w("**Historical context.** Both cases in the library closed as generator "
      "artifacts. The same caution applies to the distribution direction: it "
      "recovers shifts of 1.2–1.35x on the planted cohort, which is precisely "
      "the factor the generator applied. That is evidence the test works, and "
      "equally a reminder it has only been demonstrated against synthetic data.")
    w("")
    w("---")
    w("")
    w("## Actionable Recommendations")
    w("")
    w("**Immediate**")
    w("")
    w(f"1. **Manual review** for the {len(multi)} players corroborated by two or "
      "more directions. Independent evidence types agreeing is the escalation "
      "bar; a single direction is not.")
    w(f"2. **Enhanced monitoring** for the {len(flagged)-len(multi)} players on a "
      "single direction. No case opened.")
    w("3. **Confirm the certified math sheet.** The distribution direction is "
      "the most powerful test here and also the most dependent on the assumed "
      "outcome tables being right.")
    w("")
    w("**Longer term**")
    w("")
    w("4. **Run all directions by default, not the cheapest one.** Measured on "
      f"this data, the best single direction reaches "
      f"{max(recall(set(behav))[0], recall(sel)[0])}/{len(real)}; fused reaches "
      f"{len(tp)}/{len(real)} at {len(fp)/honest_n:.2%} false positives.")
    w("5. **Order directions by cost.** The two screening directions are single "
      "passes over the data; Monte Carlo is minutes per cohort and belongs "
      "downstream as corroboration only.")
    w("6. **Record which direction found each case** in the library. Direction-"
      "level hit rates are what tell you where the next detection gap is.")
    w("")
    w("**How this could be wrong.** The distribution test assumes rounds are "
      "independent draws from the stated tables. Any real mechanic that "
      "correlates outcomes — progressive states, must-hit-by jackpots, session "
      "streak logic — breaks that assumption and would produce shifts with no "
      "exploitation behind them.")
    w("")
    (OUT / "all_directions_report.md").write_text("\n".join(L), encoding="utf-8")

    with (OUT / "all_directions_findings.csv").open("w", newline="",
                                                    encoding="utf-8") as f:
        wtr = csv.writer(f)
        wtr.writerow(["player", "directions", "n_directions", "behavioural_rules",
                      "dist_z", "dist_shift", "dist_p", "mc_rtp", "mc_p"])
        for k in sorted(directions, key=lambda x: -len(directions[x])):
            d = dist.get(k, {})
            m = mcres.get(k, {})
            wtr.writerow([k, "|".join(sorted(directions[k])), len(directions[k]),
                          "|".join(r for r, _ in behav.get(k, [])),
                          f"{d.get('z', float('nan')):.2f}",
                          f"{d.get('shift', float('nan')):.3f}",
                          f"{d.get('p', float('nan')):.2e}",
                          f"{m.get('rtp', float('nan')):.3f}",
                          f"{m.get('p', float('nan')):.2e}"])
    print(f"\n{OUT / 'all_directions_report.md'}")


if __name__ == "__main__":
    main()
