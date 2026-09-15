"""Load BATCH-2 injects into OMG_riskdet_w1 (append only).

1. Collision check: none of the batch-2 stealth uids may already exist in w1
   (real rows or batch-1) -- else a synthetic uid would pollute a real account.
2. Append the inject_D*.csv rows via a free load job (WRITE_APPEND).

The rounds_asof TVF already points at w1, so query 53 will pick these up.
Cleanup any time: DELETE FROM OMG_riskdet_w1.rounds_all WHERE serial >= 9200000000
"""
from __future__ import annotations

import glob
import json
import pathlib

import pandas as pd

import riskdet
from riskdet.bq import CostGuardedBQ

W1_DATASET = "OMG_riskdet_w1"
OUT = pathlib.Path(__file__).resolve().parent.parent.parent / "out" / "sim"
SCHEMA_ORDER = ["game_time", "report_date", "serial", "game_seq_id", "parent",
                "uid", "game_id", "play_type", "currency", "sm_v", "sm_tag",
                "bet", "valid_bet", "win", "before_balance", "after_balance",
                "triggered", "last_modify_time"]


def main() -> None:
    cfg = riskdet.settings()
    bq = CostGuardedBQ(cfg)
    w1_table = f"{cfg.bq_project}.{W1_DATASET}.rounds_all"

    gt = json.loads((OUT / "ground_truth2.json").read_text())
    parents = sorted({g["operator"] for g in gt})
    uids = sorted({g["uid"] for g in gt})
    print(f"batch-2: {len(gt)} accounts, {len(uids)} uids, parents={parents}")

    df = bq.query_df("51_collision_check.sql",
                     {"parents": parents, "uids": uids},
                     identifiers={"rounds_table": w1_table},
                     max_bytes=200 * 1024**3, allow_large=True, stage="w1-collision2")
    if len(df):
        raise SystemExit(f"COLLISION: batch-2 uids already exist in w1:\n{df}")
    print("collision check: clean (0 existing rows for batch-2 uids)")

    frames = [pd.read_csv(f) for f in sorted(glob.glob(str(OUT / "inject_D*.csv")))]
    inj = pd.concat(frames, ignore_index=True)[SCHEMA_ORDER].copy()
    inj["game_time"] = pd.to_datetime(inj.game_time)
    inj["last_modify_time"] = pd.to_datetime(inj.last_modify_time)
    inj["report_date"] = pd.to_datetime(inj.report_date).dt.date
    inj["triggered"] = inj.triggered.astype(str).str.lower().isin(["true", "1"])
    for c in ("serial", "play_type"):
        inj[c] = inj[c].astype("int64")
    for c in ("bet", "valid_bet", "win", "before_balance", "after_balance"):
        inj[c] = inj[c].astype(float)

    n0 = bq.table_num_rows(w1_table)
    from google.cloud import bigquery
    job = bq._client.load_table_from_dataframe(
        inj, w1_table,
        job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND"))
    job.result()
    n1 = bq.table_num_rows(w1_table)
    print(f"appended {n1 - n0:,} rows (expected {len(inj):,}); w1 now {n1:,} rows")
    print(f"serial range loaded: {inj.serial.min()}–{inj.serial.max()}")


if __name__ == "__main__":
    main()
