"""Per-candidate daily drill-downs for the week-1 replay -> evidence charts.

For every human_review candidate (plus the injects, so their monitor-tier days
also show evidence) on each replay day, emit a self-contained drill-down JSON:
the account's daily series in its flagged cell, the peer/certified reference
values, and the list of families/signals that fired. The dashboard provider
reads these and renders ONLY the chart types that prove each fired family --
it needs no pandas and no BigQuery.

Real-pipeline parallel: the same JSON shape is what a targeted 42_case_daily.sql
drill-down would produce for a real human_review candidate. Here we build it
offline from out/w1/aug_daily.parquet instead of paying for a scan.

Output: out/w1/drilldowns/<case_id>@<day>.json
"""
from __future__ import annotations
import json, pathlib
import db_dtypes  # noqa
import pandas as pd
import riskdet
from riskdet.refdata import load_catalog

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
OUT_W1 = ROOT / "out" / "w1"
DD = OUT_W1 / "drilldowns"
DAYS = [f"2026-08-{d:02d}" for d in range(1, 11)]   # 08-01 .. 08-10 (full window)
INJIDS = {"ygnxzd2140", "yg0wazm8uf", "yg9q5dajs1", "gk695782579", "1041ml960"}
CELL = ["game_id", "play_type", "currency", "sm_tag"]


def _series(rows: pd.DataFrame) -> tuple[list, dict]:
    """rows = one uid+cell, one row per day, ascending. -> (day labels, series)."""
    rows = rows.sort_values("event_date")
    days = rows.event_date.astype(str).tolist()
    non_tr = (rows.rounds - rows.n_trigger).clip(lower=1)
    bet_non = (rows.sum_bet - rows.sum_bet_trigger)
    s = {
        "rtp_pct": (rows.total_win / rows.turnover.clip(lower=1e-9) * 100).round(2).tolist(),
        "trigger_rate_pct": (rows.n_trigger / rows.rounds.clip(lower=1) * 100).round(3).tolist(),
        "hit_rate_pct": (rows.n_win / rows.rounds.clip(lower=1) * 100).round(2).tolist(),
        "avg_bet_trigger": (rows.sum_bet_trigger / rows.n_trigger.clip(lower=1)).round(2).tolist(),
        "avg_bet_nontrigger": (bet_non / non_tr).round(2).tolist(),
        "max_multiple": rows.max_multiple.round(1).tolist(),
        "dup_rounds": rows.dup_seq_rounds.astype(int).tolist(),
        "balance_violations": rows.balance_violations.astype(int).tolist(),
    }
    return days, s


def main() -> None:
    cfg = riskdet.settings()
    catalog = load_catalog(cfg.catalog_csv)
    aug = pd.read_parquet(OUT_W1 / "aug_daily.parquet")
    aug["event_date"] = aug.event_date.astype(str)
    cc = pd.read_parquet(ROOT / "out" / "cell_constants_20260731T160000.parquet")
    DD.mkdir(parents=True, exist_ok=True)

    aug_uids = set(aug.uid.unique())
    n = 0
    for day in DAYS:
        cands = [json.loads(l) for l in
                 (OUT_W1 / f"candidates_{day}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        for c in cands:
            if c["uid"] not in aug_uids:
                continue                       # no daily series on disk -> no chart
            if c["escalation"] != "human_review" and c["uid"] not in INJIDS:
                continue                       # scope: human_review + injects
            # flagged cell = the play_type the signals fired on (usually base=0)
            sig_pt = next((s.get("play_type") for s in c["signals"]
                           if s.get("play_type") is not None), c.get("play_type", 0))
            rows = aug[(aug.uid == c["uid"]) & (aug.game_id == c["game_id"])
                       & (aug.play_type == sig_pt) & (aug.event_date <= day)]
            if rows.empty:
                continue
            gid, cur, tag = c["game_id"], c.get("currency"), c.get("sm_tag")
            days, series = _series(rows)

            # peer reference (leave-population baseline) for this cell
            peer = cc[(cc.game_id == gid) & (cc.play_type == sig_pt)
                      & (cc.currency == cur) & (cc.sm_tag == tag)]
            r_rtp = float(peer.rtp_emp.iloc[0]) * 100 if len(peer) else None
            r_trig = float(peer.p_trigger.iloc[0]) * 100 if len(peer) else None
            r_hit = float(peer.p_hit.iloc[0]) * 100 if len(peer) else None
            # certified references from the math sheet
            cert_rtp, _ = catalog.certified_rtp(gid, sig_pt)
            cap, _ = catalog.max_multiplier(gid, sig_pt)

            dd = {
                "case_id": c["case_id"], "day": day, "uid": c["uid"],
                "parent": c["parent"], "game_id": gid, "play_type": sig_pt,
                "currency": cur, "sm_tag": tag,
                "families": sorted(c["families"]),
                "signal_ids": sorted({s["signal_id"] for s in c["signals"]}),
                "days": days, "series": series,
                "ref": {
                    "rtp_pct": round(cert_rtp * 100, 2) if cert_rtp else r_rtp,
                    "rtp_pct_source": "certified" if cert_rtp else "peer_empirical",
                    "trigger_rate_pct": round(r_trig, 3) if r_trig is not None else None,
                    "hit_rate_pct": round(r_hit, 2) if r_hit is not None else None,
                    "max_multiple_cap": cap,
                },
                "persec": None,   # sub-second cadence lives in Cloud Logging, not BQ
            }
            (DD / f"{c['case_id']}@{day}.json").write_text(json.dumps(dd), encoding="utf-8")
            n += 1
    print(f"wrote {n} drill-downs to {DD}")


if __name__ == "__main__":
    main()
