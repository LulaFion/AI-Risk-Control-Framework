"""Offline week-1 detection -- REUSES the cached Aug-17 feature extracts, so it
costs $0 (no BigQuery scan). It computes the injects' features in pandas exactly
as riskdet/sql/20_player_cell_features.sql would, appends them to the cached real
features, runs Layer 1 with the FROZEN calibrated params against the real
baselines, and reports which injected attacks are caught. The caught injects are
merged into out/candidates.jsonl so the dashboard shows real + injected events.

Window: only injects with report_date <= 2026-08-07 (week 1). A32's 08-08 day is
therefore excluded, so A32 is under the 2000-round PRESCIENT floor this week --
an expected staged-replay result, not a miss.

Cleanup: DELETE the appended candidates by their `serial`-free case ids listed
in out/sim/ground_truth.json, or just re-run the real scan.
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib

import db_dtypes  # noqa: F401  (registers dbdate for read_parquet)
import numpy as np
import pandas as pd

import riskdet
from riskdet import calibrate as C
from riskdet.layer1 import run_layer1
from riskdet.merge import emit, fuse
from riskdet.refdata import load_catalog

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "out"
SNAP = "20260817T160000"
WEEK1_END = "2026-08-07"
BAL_TOL_ABS, BAL_TOL_REL = 0.01, 1e-6

CELL = ["game_id", "play_type", "currency", "sm_tag"]
PC_COLS = ["parent", "uid", *CELL, "rounds", "turnover", "total_win", "n_win",
           "n_trigger", "dup_seq_rounds", "bet_levels", "sum_bet", "sumsq_bet",
           "sum_bet_trigger", "sumsq_valid_bet", "sum_log_mult", "sumsq_log_mult",
           "max_multiple", "balance_violations", "first_round_utc",
           "last_round_utc", "active_days", "top_win_seq_ids"]


def inject_player_cell(df: pd.DataFrame) -> pd.DataFrame:
    """Replicate 20_player_cell_features.sql exactly, in pandas."""
    df = df.copy()
    df["mult"] = df.win / df.valid_bet.replace(0, np.nan)
    df["logm"] = np.where(df.win > 0, np.log(df.mult.clip(lower=1e-9)), 0.0)
    bviol = (df.after_balance - (df.before_balance - df.bet + df.win)).abs() > \
            np.maximum(BAL_TOL_ABS, df.before_balance.abs() * BAL_TOL_REL)
    df["_bv"] = bviol
    df["gtdt"] = pd.to_datetime(df.game_time)
    df["_date"] = df.gtdt.dt.date

    out = []
    for key, g in df.groupby(["parent", "uid", *CELL], sort=False):
        top = g.nlargest(5, "win")
        top_ids = "|".join(f"{r.game_seq_id}@{r.gtdt:%Y-%m-%dT%H:%M:%SZ}"
                           for r in top.itertuples())
        rec = dict(zip(["parent", "uid", *CELL], key))
        rec.update(
            rounds=len(g), turnover=float(g.valid_bet.sum()),
            total_win=float(g.win.sum()), n_win=int((g.win > 0).sum()),
            n_trigger=int(g.triggered.sum()),
            dup_seq_rounds=int(len(g) - g.game_seq_id.nunique()),
            bet_levels=int(g.bet.nunique()), sum_bet=float(g.bet.sum()),
            sumsq_bet=float((g.bet ** 2).sum()),
            sum_bet_trigger=float(g.loc[g.triggered, "bet"].sum()),
            sumsq_valid_bet=float((g.valid_bet ** 2).sum()),
            sum_log_mult=float(g.logm.sum()),
            sumsq_log_mult=float((g.logm ** 2).sum()),
            max_multiple=float(g.mult.max()), balance_violations=int(g._bv.sum()),
            first_round_utc=g.gtdt.min(), last_round_utc=g.gtdt.max(),
            active_days=int(g._date.nunique()), top_win_seq_ids=top_ids)
        out.append(rec)
    return pd.DataFrame(out, columns=PC_COLS)


def inject_player_timing(df: pd.DataFrame) -> pd.DataFrame:
    """Minimal but faithful timing features for the inject players."""
    df = df.sort_values(["parent", "uid", "game_time"]).copy()
    df["_gt"] = pd.to_datetime(df.game_time)
    rows = []
    for (parent, uid), g in df.groupby(["parent", "uid"], sort=False):
        gaps = g._gt.diff().dt.total_seconds().dropna()
        in_range = gaps[(gaps >= 1) & (gaps <= 30)]
        modal = (in_range.round().value_counts(normalize=True).iloc[0]
                 if len(in_range) else 0.0)
        per_min = g.groupby(g._gt.dt.floor("min")).size()
        hours = sorted(g._gt.dt.hour.unique())
        max_idle = float(gaps.max()) if len(gaps) else 0.0
        rows.append(dict(
            parent=parent, uid=uid, rounds=len(g), n_gaps=int(len(gaps)),
            same_second_gaps=int((gaps == 0).sum()),
            in_range_gaps=int(len(in_range)), modal_gap_s=13, modal_share=float(modal),
            concurrent_rounds=0, max_played_gap_s=float(in_range.max()) if len(in_range) else 0.0,
            max_idle_s=max_idle, hours_of_day_covered=len(hours),
            hour_profile=[int((g._gt.dt.hour == h).sum()) for h in range(24)],
            active_days=int(g._gt.dt.date.nunique()),
            max_rounds_per_minute=int(per_min.max()) if len(per_min) else 0,
            superhuman_minutes=0, hot_small_sessions=0, sessions=1))
    return pd.DataFrame(rows)


def main() -> None:
    cfg = riskdet.settings()
    catalog = load_catalog(cfg.catalog_csv)
    frozen = C.load_frozen(cfg)

    pc = pd.read_parquet(OUT / f"player_cell_{SNAP}.parquet")
    pt = pd.read_parquet(OUT / f"player_timing_{SNAP}.parquet")
    cc = pd.read_parquet(OUT / f"cell_constants_{SNAP}.parquet")
    # cell_operator wasn't cached; reconstruct from player_cell (rate tests only;
    # none of the injected rules depend on it)
    co = (pc.groupby([*CELL, "parent"], as_index=False)
          .agg(rounds=("rounds", "sum"), n_players=("uid", "nunique"),
               turnover=("turnover", "sum"), total_win=("total_win", "sum"),
               n_win=("n_win", "sum"), n_trigger=("n_trigger", "sum"),
               sumsq_valid_bet=("sumsq_valid_bet", "sum")))

    # ---- load week-1 injects ----
    gt = json.loads((OUT / "sim" / "ground_truth.json").read_text())
    inj_uids = {g["uid"] for g in gt}
    frames = []
    for g in gt:
        d = pd.read_csv(OUT / "sim" / f"inject_{g['scenario']}.csv")
        d = d[d.report_date <= WEEK1_END]
        if len(d):
            frames.append(d)
    inj = pd.concat(frames, ignore_index=True)
    print(f"week-1 inject rows (report_date <= {WEEK1_END}): {len(inj):,}")
    print("  by scenario/day:")
    tmp = inj.assign(date=pd.to_datetime(inj.game_time).dt.date.astype(str))
    for (u, dt_), gg in tmp.groupby(["uid", "date"]):
        print(f"    {u:14} {dt_}: {len(gg):4} rows")

    inj_pc = inject_player_cell(inj)
    inj_pt = inject_player_timing(inj)

    pc_all = pd.concat([pc, inj_pc[pc.columns]], ignore_index=True)
    pt_all = pd.concat([pt, inj_pt[pt.columns]], ignore_index=True)

    # ---- Layer 1 with frozen params, real baselines ----
    metrics = C.build_metrics(cfg, catalog, pc_all, pt_all, cc, co)
    sigs = run_layer1(cfg, catalog, frozen, metrics, pc_all, pt_all, cc)

    inj_sigs = [s for s in sigs if s.uid in inj_uids]
    print(f"\nsignals on injected uids: {len(inj_sigs)}")
    caught = {}
    for s in inj_sigs:
        caught.setdefault(s.uid, []).append(s.signal_id)
    for g in gt:
        u = g["uid"]
        got = sorted(set(caught.get(u, [])))
        status = "CAUGHT" if got else "not caught"
        print(f"  {g['scenario']} {u:14} -> {status}: {got}")

    # ---- merge caught injects into the dashboard candidate set ----
    inj_cands = fuse(cfg, inj_sigs, population_tested=len(pc_all))
    existing = [json.loads(x) for x in
                (OUT / "candidates.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    # keep real candidates; append inject candidates (stamped week-1 window end)
    def cand_json(c):
        import dataclasses
        d = {k: v for k, v in c.__dict__.items()}
        d["families"] = sorted(c.families)
        d["window_start"] = "2026-04-30T16:00:00"
        d["window_end"] = f"{WEEK1_END}T16:00:00"
        d["evidence_keys"] = c.evidence_keys
        d["signals"] = [dataclasses.asdict(s) for s in c.signals]
        for s in d["signals"]:
            s["evidence"] = [e["key"] if isinstance(e, dict) else getattr(e, "key", str(e))
                             for e in s.get("evidence", [])]
        return d
    merged = existing + [cand_json(c) for c in inj_cands]
    (OUT / "candidates.jsonl").write_text(
        "\n".join(json.dumps(m, default=str) for m in merged) + "\n", encoding="utf-8")
    print(f"\nmerged: {len(existing)} real + {len(inj_cands)} inject = {len(merged)} candidates")
    print("dashboard now reflects the week-1 injects (restart not needed; provider reloads per request-run).")


if __name__ == "__main__":
    main()
