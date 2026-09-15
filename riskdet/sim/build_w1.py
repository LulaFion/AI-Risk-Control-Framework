"""Build OMG_riskdet_w1 = copy of the real archive (already has May-Aug) + the
week-1 synthetic injects. Paid: one CTAS copy (~$0.65). Injects load via a
free load job. Collision-checked (a stealth uid must not already exist).

Cleanup: DELETE FROM OMG_riskdet_w1.rounds_all WHERE serial >= 9100000000
(or DROP the dataset).
"""
from __future__ import annotations

import datetime as _dt
import pathlib

import pandas as pd

import riskdet
from riskdet.bq import CostGuardedBQ
from riskdet.bq import sql as S

W1_DATASET = "OMG_riskdet_w1"
WEEK1_END = "2026-08-07"
OUT = pathlib.Path(__file__).resolve().parent.parent.parent / "out"

SCHEMA_ORDER = ["game_time", "report_date", "serial", "game_seq_id", "parent",
                "uid", "game_id", "play_type", "currency", "sm_v", "sm_tag",
                "bet", "valid_bet", "win", "before_balance", "after_balance",
                "triggered", "last_modify_time"]


def main() -> None:
    cfg = riskdet.settings()
    bq = CostGuardedBQ(cfg)
    w1_table = f"{cfg.bq_project}.{W1_DATASET}.rounds_all"
    ids = {"w1_table": w1_table, "rounds_table": cfg.work_table_rounds,
           "dataset": f"{cfg.bq_project}.{W1_DATASET}",
           "rounds_asof": f"{cfg.bq_project}.{W1_DATASET}.rounds_asof"}

    # ---- collision check: stealth uids must not already exist -------------
    import json
    gt = json.loads((OUT / "sim" / "ground_truth.json").read_text())
    parents = sorted({g["operator"] for g in gt})
    uids = sorted({g["uid"] for g in gt})
    df = bq.query_df("51_collision_check.sql",
                     {"parents": parents, "uids": uids},
                     max_bytes=200 * 1024**3, allow_large=True,
                     stage="w1-collision")
    if len(df):
        raise SystemExit(f"COLLISION: stealth uids already exist:\n{df}")
    print("collision check: clean (0 real rows for the 5 stealth uids)")

    # ---- build w1 = copy of archive -------------------------------------
    bq.ensure_dataset(W1_DATASET)
    bq.query_df("50_build_w1.sql", identifiers=ids, allow_large=True,
                stage="w1-build")
    n0 = bq.table_num_rows(w1_table)
    print(f"w1 copied: {n0:,} rows")

    # ---- append injects (week-1 only) via free load job -----------------
    frames = []
    for g in gt:
        d = pd.read_csv(OUT / "sim" / f"inject_{g['scenario']}.csv")
        frames.append(d[d.report_date <= WEEK1_END])
    inj = pd.concat(frames, ignore_index=True)[SCHEMA_ORDER].copy()
    inj["game_time"] = pd.to_datetime(inj.game_time)
    inj["last_modify_time"] = pd.to_datetime(inj.last_modify_time)
    inj["report_date"] = pd.to_datetime(inj.report_date).dt.date
    from google.cloud import bigquery
    job = bq._client.load_table_from_dataframe(
        inj, w1_table,
        job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND"))
    job.result()
    n1 = bq.table_num_rows(w1_table)
    print(f"injects loaded: +{n1 - n0:,} rows (week-1, report_date <= {WEEK1_END})")

    # ---- replay leakage gate in w1 (MUST point at the w1 table) ---------
    tvf_ids = {**ids, "rounds_table": w1_table}   # not the original archive!
    bq.query_df("07_replay_tvf.sql", identifiers=tvf_ids, stage="w1-tvf")
    print(f"rounds_asof created in {W1_DATASET}")
    print(f"\nw1 ready: {w1_table}  ({n1:,} rows)")


if __name__ == "__main__":
    main()
