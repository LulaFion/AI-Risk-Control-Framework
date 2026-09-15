"""Layer 1 — Behavioural rule engine, adapted to acp-develop.OMG.RecordSlot.

Ported from the sqlite risk-sandbox version. The premise is unchanged and still
correct: these rules read HOW someone plays, never how much they won. No rule
below touches RTP or net win.

WHAT CHANGED IN THE PORT (schema differences that matter)

  1. FEATURE BUY is `play_type IN (2,3,4)`. Verified from data: median bet on
     those rows is 50x-750x the base-game median, and the spin-server logs carry
     an `fbMaxAllowBet` param. play_type 0 = base, 1 = respin/gamble.

  2. FREE SPINS ARE NOT SEPARATE ROWS. One `game_seq_id` holds the whole round
     including the entire free-spin sequence (the spin-server payload nests them
     in a `res[]` array). So the old `free_share` cannot mean "share of rounds
     that are free spins" -- there is no such row. It is redefined here as the
     BONUS TRIGGER RATE (`hasFreegame='True'`), which is the closest honest
     equivalent. Interpret BEH_2 accordingly: it is trigger frequency, not
     time-spent-in-bonus.

  3. THERE IS NO session_id. Sessions are synthesised from idle gaps
     (SESSION_IDLE_MIN). Any session-based rule inherits that assumption.

  4. THERE IS NO OUTCOME/BOARD COLUMN. `outcome_x` has no equivalent: the reel
     window (`slotWindow`) exists ONLY in the acp-prod spin-server logs, not in
     BigQuery. BEH_4 therefore runs on a DEGRADED proxy -- repetition of the
     (win, bet, result_feature_code) tuple -- and is marked as such in the
     report. True board-repetition analysis needs the log path and is limited
     to the 30-day log retention window.

  5. TIMESTAMPS ARE 1-SECOND RESOLUTION. Verified: 100% of inter-round gaps are
     exact multiples of 1000ms. Sub-second cadence is NOT measurable from
     BigQuery, so BEH_3's floor is quantisation, not player behaviour. A
     companion `same_second_share` is reported because at this resolution
     "two rounds in the same second" is the only sub-second fact available.
     Millisecond timing exists in the logs (`duration` field) if needed.

  6. NO ANSWER KEY. This is production data with no ground truth, so the
     scoring/precision/recall block is gone. Layer 1 output is a candidate list
     whose job is to feed the log investigation -- which is what PRODUCES the
     labels. Until then, "precision" is unmeasured, and the report says so.

  7. CURRENCY. Player keys are parent:uid. Every rule is a RATIO, so all of them
     are currency-neutral by construction -- deliberate, since MMK and USD
     amounts differ by ~1e5 and no absolute threshold could span both.

USAGE
    # works today (no BigQuery job permission needed -- reads tabledata.list dumps)
    python detect_behavioural.py --source csv --input "slices/*.csv"

    # needs roles/bigquery.jobUser on acp-develop
    python detect_behavioural.py --source bq --days 30

Writes: output/behavioural_findings.csv, output/behavioural_report.md
"""

import argparse
import csv
import glob
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
OUT = HERE / "output"

MIN_ROUNDS = 200          # statistical floor for any per-player claim
SESSION_IDLE_MIN = 30     # idle minutes that start a new synthetic session
MAD_K = 12                # median + K*MAD, per the sandbox derivation below
GAP_CAP_S = 600           # ignore gaps over 10 min: those are breaks, not play


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------
FEATURE_SQL = """
-- Per-player behavioural features. No RTP, no net win, anywhere.
DECLARE window_days INT64 DEFAULT {days};

WITH base AS (
  SELECT
    CONCAT(parent, ':', uid) AS k, parent, uid, currency,
    ReportDate, serial, game_seq_id, game_id, game_time,
    play_type, hasFreegame, result_feature_code,
    CAST(bet      AS FLOAT64) AS bet,
    CAST(validBet AS FLOAT64) AS validBet,
    CAST(win      AS FLOAT64) AS win
  FROM `acp-develop.OMG.RecordSlot`
  WHERE ReportDate BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL window_days DAY)
                       AND CURRENT_DATE()
    AND test_demo_play = 'False'
    AND parent <> 'acdemo'
),
seq AS (
  SELECT b.*,
         DATETIME_DIFF(game_time, LAG(game_time) OVER w, SECOND) AS gap_s
  FROM base b
  WINDOW w AS (PARTITION BY k ORDER BY game_time, serial)
),
sess AS (
  SELECT s.*,
         SUM(IF(gap_s IS NULL OR gap_s > {idle}*60, 1, 0))
           OVER (PARTITION BY k ORDER BY game_time, serial) AS session_no
  FROM seq s
),
-- BEH_5: small sessions that ran hot (ratio -> currency-neutral)
hot AS (
  SELECT k, COUNTIF(n < 30 AND stake > 0 AND won > 2.0 * stake) AS hot_small
  FROM (
    SELECT k, session_no, COUNT(*) n, SUM(validBet) stake, SUM(win) won
    FROM sess GROUP BY k, session_no
  ) GROUP BY k
),
-- BEH_4: DEGRADED proxy for outcome repetition. The real signal is the reel
-- window, which is not in BigQuery -- see module docstring note 4.
dup AS (
  SELECT k, SUM(c) AS dup_rounds FROM (
    SELECT k, COUNT(*) c FROM sess
    WHERE win > 0 GROUP BY k, win, bet, result_feature_code HAVING COUNT(*) > 1
  ) GROUP BY k
)
SELECT
  s.k, ANY_VALUE(s.parent) parent, ANY_VALUE(s.uid) uid,
  ANY_VALUE(s.currency) currency,
  COUNT(*)                                   AS rounds,
  COUNTIF(s.play_type IN (2,3,4))            AS buys,
  COUNTIF(s.hasFreegame = 'True')            AS triggers,
  COUNT(DISTINCT s.bet)                      AS bet_levels,
  COUNT(DISTINCT s.session_no)               AS sessions,
  COUNT(DISTINCT s.ReportDate)               AS days,
  COUNT(DISTINCT s.game_id)                  AS games,
  AVG(IF(s.gap_s BETWEEN 0 AND {gapcap}, s.gap_s, NULL))         AS gap_mean,
  STDDEV_SAMP(IF(s.gap_s BETWEEN 0 AND {gapcap}, s.gap_s, NULL)) AS gap_sd,
  SAFE_DIVIDE(COUNTIF(s.gap_s = 0), NULLIF(COUNTIF(s.gap_s IS NOT NULL),0))
                                             AS same_second_share,
  MIN(s.game_time) AS first_round, MAX(s.game_time) AS last_round,
  COALESCE(ANY_VALUE(d.dup_rounds), 0)       AS dup_rounds,
  COALESCE(ANY_VALUE(h.hot_small), 0)        AS hot_small,
  -- evidence rounds for the log pull: "game_seq_id@UTC" (+/-1 min windows)
  ARRAY_TO_STRING(ARRAY(
    SELECT FORMAT('%s@%s', game_seq_id,
                  FORMAT_DATETIME('%Y-%m-%dT%H:%M:%SZ', game_time))
    FROM UNNEST(ARRAY_AGG(STRUCT(s.game_seq_id, s.game_time, s.win)
                          ORDER BY s.win DESC LIMIT 10))), '|') AS top_win_seq_ids
FROM sess s
LEFT JOIN dup d USING (k)
LEFT JOIN hot h USING (k)
GROUP BY s.k
HAVING rounds >= {minr}
"""


def features_from_bq(days, project="acp-develop"):
    sql = FEATURE_SQL.format(days=days, idle=SESSION_IDLE_MIN,
                             gapcap=GAP_CAP_S, minr=MIN_ROUNDS)
    proc = subprocess.run(
        ["bq", f"--project_id={project}", "query", "--use_legacy_sql=false",
         "--format=csv", "--max_rows=100000"],
        input=sql, capture_output=True, text=True)
    if proc.returncode != 0:
        err = proc.stderr or ""
        if "jobs.create" in err or "Access Denied" in err:
            sys.exit("BLOCKED: this principal cannot create BigQuery jobs.\n"
                     "  gcloud projects add-iam-policy-binding acp-develop \\\n"
                     "    --member='serviceAccount:airc-740@acp-prod.iam."
                     "gserviceaccount.com' \\\n"
                     "    --role='roles/bigquery.jobUser'\n"
                     "Meanwhile use: --source csv")
        sys.exit(f"bq failed: {err[:600]}")
    return pd.read_csv(StringIO(proc.stdout))


def features_from_csv(pattern):
    """Same features, computed locally from tabledata.list dumps (bq head)."""
    files = sorted(glob.glob(pattern))
    if not files:
        sys.exit(f"no files matched {pattern}")
    d = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    d = d[(d.test_demo_play == False) | (d.test_demo_play == "False")]
    d = d[d.parent != "acdemo"]
    missing = {"serial", "play_type", "bet", "validBet", "win",
               "game_seq_id"} - set(d.columns)
    if missing:
        sys.exit(f"dump is missing required columns: {sorted(missing)}")
    if "result_feature_code" not in d.columns:
        # BEH_4's proxy degrades further without it -- (win, bet) only.
        print("  WARNING: no result_feature_code in dump; BEH_4 proxy weakened")
        d["result_feature_code"] = 0
    d["game_time"] = pd.to_datetime(d.game_time)
    d["k"] = d.parent.astype(str) + ":" + d.uid.astype(str)
    d = d.sort_values(["k", "game_time", "serial"]).reset_index(drop=True)

    g = d.groupby("k", sort=False)
    d["gap_s"] = g.game_time.diff().dt.total_seconds()
    d["session_no"] = g.gap_s.transform(
        lambda s: (s.isna() | (s > SESSION_IDLE_MIN * 60)).cumsum())
    d["is_buy"] = d.play_type.isin([2, 3, 4])
    d["is_trigger"] = (d.hasFreegame == True) | (d.hasFreegame == "True")
    gap = d.gap_s.where(d.gap_s.between(0, GAP_CAP_S))

    f = d.groupby("k").agg(
        parent=("parent", "first"), uid=("uid", "first"),
        currency=("currency", "first"),
        rounds=("serial", "size"), buys=("is_buy", "sum"),
        triggers=("is_trigger", "sum"), bet_levels=("bet", "nunique"),
        sessions=("session_no", "nunique"), days=("ReportDate", "nunique"),
        games=("game_id", "nunique"),
        first_round=("game_time", "min"), last_round=("game_time", "max"),
    )
    f["gap_mean"] = gap.groupby(d.k).mean()
    f["gap_sd"] = gap.groupby(d.k).std()
    f["same_second_share"] = (d.gap_s == 0).groupby(d.k).sum() / \
                             d.gap_s.notna().groupby(d.k).sum().replace(0, np.nan)

    # BEH_4 degraded proxy (see docstring note 4)
    w = d[d.win > 0]
    dupc = w.groupby(["k", "win", "bet", "result_feature_code"]).size()
    f["dup_rounds"] = dupc[dupc > 1].groupby("k").sum()

    # BEH_5 hot small sessions
    s = d.groupby(["k", "session_no"]).agg(n=("serial", "size"),
                                           stake=("validBet", "sum"),
                                           won=("win", "sum"))
    f["hot_small"] = ((s.n < 30) & (s.stake > 0) &
                      (s.won > 2.0 * s.stake)).groupby("k").sum()

    ev = (d.sort_values("win", ascending=False).groupby("k").head(10)
            .groupby("k")
            .apply(lambda x: "|".join(
                f"{r.game_seq_id}@{r.game_time:%Y-%m-%dT%H:%M:%SZ}"
                for r in x.itertuples()), include_groups=False))
    f["top_win_seq_ids"] = ev

    f = f.fillna({"dup_rounds": 0, "hot_small": 0, "same_second_share": 0.0})
    f = f[f.rounds >= MIN_ROUNDS].reset_index()
    return f


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
def rules(pl, pop):
    hits = []

    # BEH_1 -- stake routed through the bought feature far beyond the norm
    if pl["buy_share"] >= pop["buy_share"]:
        hits.append(("BEH_1", "feature routing: "
                     f"{pl['buy_share']:.1%} of rounds are feature buys "
                     f"(threshold {pop['buy_share']:.1%})"))

    # BEH_2 -- bonus triggers at a rate the game should not produce.
    # NOTE: trigger RATE, not time-in-bonus -- free spins are not separate rows.
    if pl["trigger_share"] >= pop["trigger_share"]:
        hits.append(("BEH_2", "bonus trigger rate: "
                     f"{pl['trigger_share']:.2%} of rounds trigger a free game "
                     f"(threshold {pop['trigger_share']:.2%})"))

    # BEH_3 -- machine-regular timing. Stake count is deliberately NOT a
    # condition: a bot that varies its stake between sessions is still a bot.
    # CAVEAT: game_time is 1-second resolution, so sd cannot resolve below ~1s.
    if pl["gap_sd"] <= 1.0 and pl["rounds"] >= 500:
        hits.append(("BEH_3", "automation signature: inter-round gap sd "
                     f"{pl['gap_sd']:.2f}s (mean {pl['gap_mean']:.1f}s) over "
                     f"{int(pl['rounds']):,} rounds"))

    # BEH_3b -- the only sub-second fact available at 1s resolution
    if pl["same_second_share"] >= pop["same_second_share"] and pl["rounds"] >= 500:
        hits.append(("BEH_3b", "burst play: "
                     f"{pl['same_second_share']:.1%} of rounds land in the same "
                     f"second as the previous one "
                     f"(threshold {pop['same_second_share']:.1%})"))

    # BEH_4 -- DEGRADED: (win, bet, feature_code) repetition, not board repetition
    if pl["dup_share"] >= pop["dup_share"] and pl["dup_rounds"] >= 20:
        hits.append(("BEH_4", "outcome repetition (proxy): "
                     f"{int(pl['dup_rounds'])} winning rounds "
                     f"({pl['dup_share']:.1%}) repeat an identical "
                     f"(win, bet, feature) tuple "
                     f"(threshold {pop['dup_share']:.1%})"))

    # BEH_5 -- recurring tiny sessions that run hot
    if pl["hot_small"] >= 3:
        hits.append(("BEH_5", f"{int(pl['hot_small'])} sessions under 30 rounds "
                     "returned over 2x stake"))
    return hits


def robust(values, k, floor, label):
    """Median + k*MAD, with a documented fallback when MAD collapses.

    Why not a high percentile: a p99.9 cutoff is contaminated by the very
    players it is meant to find. In the sandbox run, with ~30 abusers in ~2,000
    players the 99.9th percentile WAS the abuser group -- the threshold landed
    at their median (buy_share p99.9 = 9.23% vs abuser median 9.24%) and recall
    collapsed to 5/26. Median and MAD are set by the honest bulk and cannot be
    dragged by a small contaminated tail.

    MAD-COLLAPSE (new, and it bites on this data): when more than half the
    population shares one value, MAD is exactly 0 and median + k*MAD degenerates
    to the median itself, which would flag every nonzero player. That is the
    normal case here -- most players never buy a feature, so buy_share has
    median 0 and MAD 0. Fall back to the spread of the NONZERO population.
    """
    v = np.asarray(values, float)
    v = v[np.isfinite(v)]
    med = float(np.median(v))
    mad = float(np.median(np.abs(v - med)))
    if mad > 0:
        thr, how = med + k * mad, f"median+{k}MAD"
    else:
        nz = v[v > med]
        if nz.size >= 20:
            nzmed = float(np.median(nz))
            nzmad = float(np.median(np.abs(nz - nzmed))) or float(nz.std()) or 1e-9
            thr, how = nzmed + k * nzmad, f"MAD=0 -> nonzero median+{k}MAD"
        else:
            thr, how = float(np.quantile(v, 0.999)), "MAD=0 -> p99.9"
    return max(floor, thr), med, mad, how


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["csv", "bq"], default="csv")
    ap.add_argument("--input", default="slices/*.csv",
                    help="csv mode: glob of bq-head dumps")
    ap.add_argument("--days", type=int, default=30,
                    help="bq mode: lookback (keep <=30, log retention)")
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)

    print(f"building features ({a.source}) ...")
    F = features_from_bq(a.days) if a.source == "bq" else features_from_csv(a.input)
    n = F.rounds.clip(lower=1)
    F["buy_share"] = F.buys / n
    F["trigger_share"] = F.triggers / n
    F["dup_share"] = F.dup_rounds / n
    F["gap_sd"] = F.gap_sd.fillna(999.0)
    F["gap_mean"] = F.gap_mean.fillna(999.0)
    print(f"  {len(F):,} eligible players (>= {MIN_ROUNDS} rounds)")
    if F.empty:
        sys.exit("no eligible players")

    pop, diag = {}, []
    for key, floor, label in (("buy_share", 0.03, "feature-buy share"),
                              ("trigger_share", 0.05, "bonus trigger rate"),
                              ("dup_share", 0.05, "outcome repetition"),
                              ("same_second_share", 0.20, "same-second bursts")):
        thr, med, mad, how = robust(F[key], MAD_K, floor, label)
        pop[key] = thr
        diag.append((label, med, mad, thr, how))
        print(f"  {label:22} median {med:.3%}  MAD {mad:.3%}  "
              f"-> threshold {thr:.2%}  [{how}]")

    flagged = {}
    for _, p in F.iterrows():
        h = rules(p, pop)
        if h:
            flagged[p["k"]] = h

    fired = defaultdict(int)
    for hs in flagged.values():
        for r, _ in hs:
            fired[r] += 1
    multi = {k: h for k, h in flagged.items() if len(h) >= 2}

    print(f"\nflagged {len(flagged)} of {len(F):,} players "
          f"({len(flagged)/len(F):.1%}); {len(multi)} fired 2+ rules")
    for r in sorted(fired):
        print(f"    {r:7} {fired[r]}")

    # ------------------------------------------------------------- report --
    L = []; w = L.append
    w("# Layer 1 — Behavioural Rule Engine")
    w("")
    w(f"- run: `{datetime.now().isoformat(timespec='seconds')}`")
    w(f"- source: `{a.source}` · {len(F):,} eligible players "
      f"(>= {MIN_ROUNDS} rounds)")
    w("")
    w("**Premise.** Every rule reads *how* someone plays, never how much they "
      "won. No rule below touches RTP or net win. Rules are ratios, which makes "
      "them currency-neutral — necessary here, since MMK and USD amounts differ "
      "by ~1e5 and no absolute threshold could span both.")
    w("")
    w("> Analysis support, not a verdict. A behavioural match indicates a "
      "pattern worth a human look — it is not evidence of intent.")
    w("")
    w("## Thresholds")
    w("")
    w("Median + 12·MAD of the eligible population — never a high percentile. A "
      "p99.9 cutoff is contaminated by the very players it is meant to find: in "
      "the sandbox derivation, with ~30 abusers among ~2,000 players the 99.9th "
      "percentile *was* the abuser group, so the threshold landed at their "
      "median and recall collapsed to 5/26.")
    w("")
    w("**MAD collapse.** Where most players share one value (most never buy a "
      "feature), MAD is exactly 0 and median+k·MAD degenerates to the median, "
      "which would flag every nonzero player. Those rows fall back to the "
      "spread of the nonzero population — shown in the last column.")
    w("")
    w("| Feature | Median | MAD | Threshold | Method |")
    w("|---|---:|---:|---:|---|")
    for label, med, mad, thr, how in diag:
        w(f"| {label} | {med:.3%} | {mad:.3%} | **{thr:.2%}** | {how} |")
    w("")
    w("## Rules")
    w("")
    w("| Rule | Signal | Fired |")
    w("|---|---|---:|")
    w(f"| `BEH_1` | feature-buy share of rounds | {fired['BEH_1']} |")
    w(f"| `BEH_2` | bonus trigger rate | {fired['BEH_2']} |")
    w(f"| `BEH_3` | inter-round gap sd ≤ 1.0s over ≥500 rounds | {fired['BEH_3']} |")
    w(f"| `BEH_3b` | same-second bursts | {fired['BEH_3b']} |")
    w(f"| `BEH_4` | outcome repetition *(degraded proxy)* | {fired['BEH_4']} |")
    w(f"| `BEH_5` | small sessions returning >2x | {fired['BEH_5']} |")
    w("")
    w(f"**{len(flagged)} of {len(F):,} players flagged "
      f"({len(flagged)/len(F):.1%}); {len(multi)} fired two or more rules.** "
      "Review the multi-rule set first — a single behavioural rule is one signal.")
    w("")
    w("## Known limits of this port")
    w("")
    w("1. **`BEH_4` is degraded.** The real signal is board repetition, and the "
      "reel window (`slotWindow`) exists only in the acp-prod spin-server logs, "
      "not in BigQuery. The proxy here repeats on `(win, bet, "
      "result_feature_code)`, which a legitimate player hitting the same small "
      "payout repeatedly will also trip. True board analysis requires the log "
      "path and is bounded by 30-day log retention.")
    w("2. **`BEH_3` cannot resolve below 1 second.** `game_time` is "
      "second-granular — 100% of gaps are exact multiples of 1000ms — so the sd "
      "floor is quantisation, not behaviour. Millisecond timing exists in the "
      "logs (`duration`) if this rule needs sharpening.")
    w("3. **`BEH_2` is trigger *rate*, not time-in-bonus.** Free spins are not "
      "separate rows; one `game_seq_id` contains the whole round.")
    w("4. **Sessions are synthesised** from "
      f"{SESSION_IDLE_MIN}-minute idle gaps. There is no `session_id` in the "
      "source, so `BEH_5` inherits that assumption.")
    w("5. **No ground truth.** Precision and recall are unmeasured on production "
      "data. That is what the log investigation is for: each candidate resolves "
      "to confirmed or cleared, and those outcomes become the first real labels. "
      "Do not quote a hit rate until they exist.")
    w("")
    w("## Next step")
    w("")
    w("`behavioural_findings.csv` carries `top_win_seq_ids` as "
      "`game_seq_id@timestamp`, so the log puller opens a ±1 minute window "
      "around each round:")
    w("")
    w("```")
    w("python scripts/pull_logs.py --input output/behavioural_findings.csv \\")
    w("    --out output/logs --pad-min 1")
    w("```")
    (OUT / "behavioural_report.md").write_text("\n".join(L), encoding="utf-8")

    idx = F.set_index("k")
    with (OUT / "behavioural_findings.csv").open("w", newline="",
                                                 encoding="utf-8") as f:
        wtr = csv.writer(f)
        wtr.writerow(["parent", "uid", "currency", "rules", "n_rules", "rounds",
                      "buy_share", "trigger_share", "gap_sd", "gap_mean",
                      "same_second_share", "bet_levels", "sessions", "games",
                      "dup_rounds", "hot_small", "evidence",
                      "log_window_start_utc", "log_window_end_utc",
                      "top_win_seq_ids"])
        for k, hs in sorted(flagged.items(), key=lambda kv: -len(kv[1])):
            p = idx.loc[k]
            wtr.writerow([p["parent"], p["uid"], p["currency"],
                          "|".join(r for r, _ in hs), len(hs), int(p["rounds"]),
                          f"{p['buy_share']:.4f}", f"{p['trigger_share']:.4f}",
                          f"{p['gap_sd']:.2f}", f"{p['gap_mean']:.2f}",
                          f"{p['same_second_share']:.4f}",
                          p["bet_levels"], p["sessions"], p["games"],
                          int(p["dup_rounds"]), int(p["hot_small"]),
                          " ; ".join(d for _, d in hs),
                          pd.to_datetime(p["first_round"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          pd.to_datetime(p["last_round"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          p.get("top_win_seq_ids", "")])
    print(f"\n{OUT / 'behavioural_report.md'}")
    print(f"{OUT / 'behavioural_findings.csv'}")


if __name__ == "__main__":
    main()
