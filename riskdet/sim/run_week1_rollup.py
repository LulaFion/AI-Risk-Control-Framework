"""Per-day detection on OMG_riskdet_w1 via ONE-read + additive roll-up (~$1.3).

Paid reads (once each):
  A) baseline features at as_of 2026-07-31 (pre-August)  -- ~$1.2 (May-Jul)
  B) August-1..7 per-(player,cell,DAY) stats from w1      -- ~$0.1 (Aug partitions)

Then FREE per-day, D in 08-01..08-07:
  player_cell(D) = baseline(07-31)  +  sum(August daily rows where day <= D)
  (additive: counts/sums/sumsq roll up exactly; injects are in w1 so they
   appear on their true days -> exact detection latency)
  cell_constants/operator = baseline (August moves them <0.01%, immaterial)
  -> run Layer 1 with frozen params -> that day's candidates
Snapshots: out/w1/candidates_<D>.jsonl, output/w1/case_queue_<D>.md
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib

os.environ["RISKDET_WORK_DATASET"] = "OMG_riskdet_w1"

import db_dtypes  # noqa: F401
import numpy as np
import pandas as pd

import riskdet
from riskdet import calibrate as C
from riskdet.bq import CostGuardedBQ
from riskdet.layer1 import run_layer1
from riskdet.merge import fuse
from riskdet.pipeline.features import extract
from riskdet.pipeline.run import _cell_operator
from riskdet.refdata import load_catalog

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
OUT, OUT_W1 = ROOT / "out", ROOT / "out" / "w1"
ART_W1 = ROOT / "output" / "w1"
DAYS = [f"2026-08-0{d}" for d in range(1, 8)]
BASE_ASOF = _dt.datetime(2026, 7, 31, 16, 0, 0)
CELL = ["game_id", "play_type", "currency", "sm_tag"]
KEY = ["parent", "uid", *CELL]
ADD = ["rounds", "turnover", "total_win", "n_win", "n_trigger", "dup_seq_rounds",
       "sum_bet", "sumsq_bet", "sum_bet_trigger", "sumsq_valid_bet",
       "sum_log_mult", "sumsq_log_mult", "balance_violations"]


def rollup(base_pc: pd.DataFrame, aug: pd.DataFrame, day: str) -> pd.DataFrame:
    """Exact cumulative player_cell as of end-of-day `day`."""
    a = aug[aug.event_date.astype(str) <= day]
    if len(a):
        g = a.groupby(KEY, as_index=False).agg(
            {**{c: "sum" for c in ADD},
             "max_multiple": "max", "bet_levels_day": "max",
             "first_round_utc": "min", "last_round_utc": "max"})
        # top_win_seq_ids: pick the day-cell with the biggest single win's list
        tops = (a.sort_values("total_win", ascending=False)
                .groupby(KEY, as_index=False).first()[KEY + ["top_win_seq_ids"]])
        g = g.merge(tops, on=KEY, how="left")
    else:
        g = pd.DataFrame(columns=base_pc.columns)

    # merge august increments onto the 07-31 baseline
    merged = base_pc.set_index(KEY).copy()
    for _, r in g.iterrows():
        k = tuple(r[c] for c in KEY)
        if k in merged.index:
            for c in ADD:
                merged.at[k, c] = merged.at[k, c] + r[c]
            merged.at[k, "max_multiple"] = max(merged.at[k, "max_multiple"], r.max_multiple)
            merged.at[k, "active_days"] = merged.at[k, "active_days"] + 1
            merged.at[k, "last_round_utc"] = r.last_round_utc
        else:
            row = {c: r.get(c, 0) for c in base_pc.columns if c not in KEY}
            row.update(active_days=1, bet_levels=int(r.bet_levels_day),
                       top_win_seq_ids=r.get("top_win_seq_ids", ""))
            merged.loc[k, list(row.keys())] = list(row.values())
    return merged.reset_index()


def to_json(c, day: str) -> dict:
    import dataclasses
    d = {k: v for k, v in c.__dict__.items()}
    d["families"] = sorted(c.families)
    d["window_start"] = "2026-04-30T16:00:00"
    d["window_end"] = f"{day}T16:00:00"
    d["evidence_keys"] = c.evidence_keys
    d["signals"] = [dataclasses.asdict(s) for s in c.signals]
    for s in d["signals"]:
        s["evidence"] = [e.get("key") if isinstance(e, dict) else str(e)
                         for e in s.get("evidence", [])]
    return d


def main() -> None:
    cfg = riskdet.settings()
    catalog = load_catalog(cfg.catalog_csv)
    frozen = C.load_frozen(cfg)
    bq = CostGuardedBQ(cfg)
    OUT_W1.mkdir(parents=True, exist_ok=True)
    ART_W1.mkdir(parents=True, exist_ok=True)

    snap = "20260731T160000"
    base_p = OUT / f"player_cell_{snap}.parquet"
    if base_p.is_file():   # REUSE the already-paid baseline extract
        print("A) baseline @ 2026-07-31 -- reusing cached parquets (no re-charge)")
        base_pc = pd.read_parquet(base_p)
        base_pt = pd.read_parquet(OUT / f"player_timing_{snap}.parquet")
        cc = pd.read_parquet(OUT / f"cell_constants_{snap}.parquet")
    else:
        print("A) baseline features @ 2026-07-31 (w1) ...")
        arts = extract(bq, BASE_ASOF, stage="w1-baseline")
        base_pc = pd.read_parquet(arts.player_cell)
        base_pt = pd.read_parquet(arts.player_timing)
        cc = pd.read_parquet(arts.cell_constants)
    # cell_operator: reconstruct from baseline player_cell (free; injects don't use it)
    co = (base_pc.groupby([*CELL, "parent"], as_index=False)
          .agg(rounds=("rounds", "sum"), n_players=("uid", "nunique"),
               turnover=("turnover", "sum"), total_win=("total_win", "sum"),
               n_win=("n_win", "sum"), n_trigger=("n_trigger", "sum"),
               sumsq_valid_bet=("sumsq_valid_bet", "sum")))
    print(f"   baseline player_cell={len(base_pc):,} cells")

    print("B) August-1..7 daily-grained player_cell from w1 ...")
    aug = bq.query_df("53_player_cell_daily_aug.sql",
                      {"as_of_time": _dt.datetime(2026, 8, 7, 23, 59, 59)},
                      allow_large=True, stage="w1-aug-daily")
    aug.to_parquet(OUT_W1 / "aug_daily.parquet", index=False)
    print(f"   august daily rows={len(aug):,}; total cost ~${cfg.usd(bq.budget.spent_bytes):.2f}")

    ledger = []
    for day in DAYS:
        pc_D = rollup(base_pc, aug, day)
        pc_D = pc_D[pc_D.rounds >= cfg.thresholds["global"]["min_rounds_per_cell"]]
        metrics = C.build_metrics(cfg, catalog, pc_D, base_pt, cc, co)
        sigs = run_layer1(cfg, catalog, frozen, metrics, pc_D, base_pt, cc)
        cands = fuse(cfg, sigs, population_tested=int(pc_D.groupby(["parent", "uid"]).ngroups))
        rows = [to_json(c, day) for c in cands]
        (OUT_W1 / f"candidates_{day}.jsonl").write_text(
            "\n".join(json.dumps(r, default=str) for r in rows) + "\n", encoding="utf-8")
        injids = {"ygnxzd2140", "yg0wazm8uf", "yg9q5dajs1", "gk695782579", "1041ml960"}
        caught = sorted({c.uid for c in cands if c.uid in injids})
        ledger.append({"day": day, "candidates": len(cands),
                       "human_review": sum(1 for c in cands if c.escalation == "human_review"),
                       "injects_detected": caught})
        print(f"  {day}: candidates={len(cands):4} injects_caught={caught}")

    (OUT_W1 / "daily_ledger.json").write_text(json.dumps(ledger, indent=2))
    print(f"\ntotal BigQuery ~${cfg.usd(bq.budget.spent_bytes):.2f}")
    print("per-day injects detected:")
    for r in ledger:
        print(f"  {r['day']}: {r['injects_detected']}")


if __name__ == "__main__":
    main()
