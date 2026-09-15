"""Fast, vectorized per-day roll-up (fixes the slow/buggy version).
Reuses cached 07-31 baseline; re-runs the (now TVF-fixed) August extract once
(~$0.03); rolls up per day 08-01..08-03 and runs Layer 1. Writes per-day
candidate sets + reports inject detection with the min_rounds-floor caveat.
"""
from __future__ import annotations
import datetime as _dt, json, os, pathlib
os.environ["RISKDET_WORK_DATASET"] = "OMG_riskdet_w1"
import db_dtypes  # noqa
import numpy as np, pandas as pd
import riskdet
from riskdet import calibrate as C
from riskdet.bq import CostGuardedBQ
from riskdet.casestore import CaseStore
from riskdet.layer1 import run_layer1
from riskdet.layer2 import run_layer2
from riskdet.layer3 import detect_cohorts, membership, run_ml_discovery
from riskdet.merge import fuse
from riskdet.refdata import load_catalog

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
OUT, OUT_W1 = ROOT / "out", ROOT / "out" / "w1"
DAYS = [f"2026-08-{d:02d}" for d in range(1, 11)]   # 08-01 .. 08-10 (full window)
SNAP = "20260731T160000"
CELL = ["game_id", "play_type", "currency", "sm_tag"]
KEY = ["parent", "uid", *CELL]
ADD = ["rounds", "turnover", "total_win", "n_win", "n_trigger", "dup_seq_rounds",
       "sum_bet", "sumsq_bet", "sum_bet_trigger", "sumsq_valid_bet",
       "sum_log_mult", "sumsq_log_mult", "balance_violations"]
INJIDS = {"ygnxzd2140": "A01", "yg0wazm8uf": "A09", "yg9q5dajs1": "A32",
          "gk695782579": "A36", "1041ml960": "A23"}


def to_json(c, day):
    import dataclasses
    d = {k: v for k, v in c.__dict__.items()}
    d["families"] = sorted(c.families)
    d["window_start"] = "2026-04-30T16:00:00"
    d["window_end"] = f"{day}T16:00:00"
    d["evidence_keys"] = c.evidence_keys
    d["signals"] = [dataclasses.asdict(s) for s in c.signals]
    for s in d["signals"]:
        s["evidence"] = [e.get("key") if isinstance(e, dict) else str(e) for e in s.get("evidence", [])]
    return d


def _load_dispositions() -> dict:
    """case_id -> latest human decision recorded in the dashboard (if any)."""
    out, ddir = {}, ROOT / "output" / "dispositions"
    if ddir.is_dir():
        for p in ddir.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            cid = d.get("case_id") or d.get("event_id", "").split("@")[0]
            if cid and d.get("decision"):
                out[cid] = d["decision"]
    return out


def main():
    cfg = riskdet.settings()
    catalog = load_catalog(cfg.catalog_csv)
    frozen = C.load_frozen(cfg)
    min_rounds = cfg.thresholds["global"]["min_rounds_per_cell"]
    bq = CostGuardedBQ(cfg)

    base_pc = pd.read_parquet(OUT / f"player_cell_{SNAP}.parquet")
    base_pt = pd.read_parquet(OUT / f"player_timing_{SNAP}.parquet")
    cc = pd.read_parquet(OUT / f"cell_constants_{SNAP}.parquet")
    co = (base_pc.groupby([*CELL, "parent"], as_index=False)
          .agg(rounds=("rounds", "sum"), n_players=("uid", "nunique"),
               turnover=("turnover", "sum"), total_win=("total_win", "sum"),
               n_win=("n_win", "sum"), n_trigger=("n_trigger", "sum"),
               sumsq_valid_bet=("sumsq_valid_bet", "sum")))

    aug_p = OUT_W1 / "aug_daily.parquet"
    if aug_p.is_file():                       # REUSE cached extract (no re-scan, no cost)
        print("August extract -- reusing cached aug_daily.parquet (no re-scan)")
        aug = pd.read_parquet(aug_p)
    else:
        print("re-running August extract via fixed TVF (as_of 08-10) ...")
        aug = bq.query_df("53_player_cell_daily_aug.sql",
                          {"as_of_time": _dt.datetime(2026, 8, 10, 23, 59, 59)},
                          allow_large=True, stage="w1-aug-daily-wk2")
        aug.to_parquet(aug_p, index=False)
    aug["event_date"] = aug.event_date.astype(str)
    print(f"  august rows={len(aug):,}, days={sorted(aug.event_date.unique())}, "
          f"injects={aug.uid.isin(INJIDS).sum()}, cost ~${cfg.usd(bq.budget.spent_bytes):.2f}")

    def build_pc_full(a):
        """Cumulative per-cell frame = 07-31 baseline + August increment (<= day)."""
        cum = a.groupby(KEY, as_index=False).agg(
            {**{c: "sum" for c in ADD}, "max_multiple": "max",
             "bet_levels_day": "max", "last_round_utc": "max",
             "first_round_utc": "min"})
        tops = (a.sort_values("total_win", ascending=False)
                .drop_duplicates(KEY)[KEY + ["top_win_seq_ids"]])
        days_ct = a.groupby(KEY, as_index=False).event_date.nunique().rename(
            columns={"event_date": "aug_days"})
        cum = cum.merge(tops, on=KEY, how="left").merge(days_ct, on=KEY, how="left")
        m = base_pc.merge(cum, on=KEY, how="outer", suffixes=("", "_a"))
        for c in ADD:
            m[c] = m[c].fillna(0) + m[f"{c}_a"].fillna(0)
        m["max_multiple"] = m[["max_multiple", "max_multiple_a"]].max(axis=1)
        m["active_days"] = m["active_days"].fillna(0) + m["aug_days"].fillna(0)
        m["bet_levels"] = m[["bet_levels", "bet_levels_day"]].max(axis=1).fillna(2)
        m["top_win_seq_ids"] = m["top_win_seq_ids_a"].fillna(m["top_win_seq_ids"]).fillna("")
        for tcol in ("first_round_utc", "last_round_utc"):
            m[tcol] = m[f"{tcol}"].fillna(m[f"{tcol}_a"])
        return m[base_pc.columns]

    # Cohorts are structural (uid skeletons, births, behavioural identity) -- they
    # don't change day to day, so compute ONCE on the full-window frame and reuse
    # the membership for every day. Per-day cohort detection was the bottleneck.
    print("detecting cohorts once on full window ...")
    cmemb = membership(detect_cohorts(cfg, catalog, build_pc_full(aug), base_pt, cc))
    print(f"  cohort members: {len({u for (_p, u) in cmemb})}")

    # State gate: a scan is stateless and re-emits every standing candidate, so
    # a case would otherwise re-appear in the queue every single day. The store
    # turns per-day candidates into stateful cases and surfaces only what is NEW
    # (opened / escalated / reopened). It sits under any cadence -- daily here,
    # hourly/minute later -- and is the single dedup point. It never enforces.
    store_path = OUT_W1 / "case_state.json"
    if store_path.is_file():
        store_path.unlink()                              # rebuild cleanly each full replay
    store = CaseStore(store_path)
    dispositions = _load_dispositions()

    ledger = []
    for day in DAYS:
        a = aug[aug.event_date <= day]
        pc_full = build_pc_full(a)                       # ungated: every cell
        pc_D = pc_full[pc_full.rounds >= min_rounds]     # gated: query-20 HAVING

        metrics = C.build_metrics(cfg, catalog, pc_D, base_pt, cc, co)
        # statistical/volume rules on the gated frame; absolute INTEGRITY rules on
        # the ungated frame (pre-gate pass) so low-volume integrity attacks below
        # the 200-round floor are still caught.
        sigs = run_layer1(cfg, catalog, frozen, metrics, pc_D, base_pt, cc,
                          integrity_pc=pc_full)
        # Layer 2: game-cell aggregate drift over the daily series (a deploy /
        # config RTP shift that no single player carries). Built from the daily
        # per-cell sums; catches a cohort that dominates a low-volume cell.
        gday = a.groupby(CELL + ["event_date"], as_index=False).agg(
            rounds=("rounds", "sum"), total_win=("total_win", "sum"),
            turnover=("turnover", "sum"), n_trigger=("n_trigger", "sum"),
            sumsq_valid_bet=("sumsq_valid_bet", "sum"))
        l2_sigs = run_layer2(cfg, catalog, gday)
        sigs += l2_sigs
        # Layer 3: unsupervised catch-all on the UNGATED frame, for accounts NO
        # rule flagged (correlated-by-construction otherwise). "Unusual", not
        # risky -> monitor/watch tier via fuse; validation gate may suppress.
        rule_flagged = {(s.parent, s.uid) for s in sigs if s.uid}
        ml_sigs, mlval = run_ml_discovery(cfg, pc_full, base_pt, rule_flagged,
                                          contamination=0.001)
        sigs += ml_sigs
        # cohort membership (computed once above) feeds fuse for dual-baseline
        # annotation -- not a filter; a human approves any exclusion.
        pop = int(pc_D.groupby(["parent", "uid"]).ngroups)
        cands = fuse(cfg, sigs, cohort_membership=cmemb, population_tested=pop)
        rows = [to_json(c, day) for c in cands]
        (OUT_W1 / f"candidates_{day}.jsonl").write_text(
            "\n".join(json.dumps(r, default=str) for r in rows) + "\n", encoding="utf-8")

        # State gate: candidates_<day> is the full cumulative queue (audit); the
        # gate distils it to the de-duplicated alert queue a human should see.
        alerts = store.ingest(day, rows, dispositions=dispositions)
        (OUT_W1 / f"alerts_{day}.jsonl").write_text(
            "".join(json.dumps(a, default=str) + "\n" for a in alerts), encoding="utf-8")
        # Auto-route to Layer 4: human_review alerts enter the investigation queue
        # automatically (dedup by state). Cases that already have a Layer-4 case
        # file are marked investigated so they leave the pending queue.
        for c in rows:
            if (ROOT / "output" / "cases" / f"{c['case_id']}.md").is_file():
                store.mark_investigated(c["case_id"], day)
        l4_new = [a for a in alerts if a.get("l4_route")]
        (OUT_W1 / f"l4_queue_{day}.jsonl").write_text(
            "".join(json.dumps(a, default=str) + "\n" for a in l4_new), encoding="utf-8")

        det = {INJIDS[c.uid]: sorted({s.signal_id for s in c.signals})
               for c in cands if c.uid in INJIDS}
        # present in the ungated frame at all (integrity runs pre-gate now)
        present = {INJIDS[u] for u in INJIDS if (pc_full.uid == u).any()}
        ledger.append({"day": day, "candidates": len(cands),
                       "alerts": len(alerts), "l4_routed": len(l4_new),
                       "l4_pending": len(store.layer4_queue()),
                       "human_review": sum(1 for c in cands if c.escalation == "human_review"),
                       "injects_detected": det, "injects_eligible": sorted(present)})
        l2 = sorted({s.signal_id for s in l2_sigs})
        print(f"  {day}: cands={len(cands)} alerts={len(alerts)} "
              f"L4+{len(l4_new)}(pending {len(store.layer4_queue())}) "
              f"ml={len(ml_sigs)}({mlval.verdict}) "
              f"l2={len(l2_sigs)}{l2} detected={det}", flush=True)

    store.save()
    (OUT_W1 / "daily_ledger.json").write_text(json.dumps(ledger, indent=2))
    print(f"\ntotal new cost ~${cfg.usd(bq.budget.spent_bytes):.2f}")


if __name__ == "__main__":
    main()
