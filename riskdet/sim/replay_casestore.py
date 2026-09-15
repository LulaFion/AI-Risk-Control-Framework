"""Replay the 10-day week-1 candidate files through the case-store gate.

Demonstrates the dedup: feeds each day's candidates_<date>.jsonl into a single
persistent CaseStore (as a real daily scheduler would), and writes:
    out/w1/alerts_<date>.jsonl   the DE-DUPLICATED alert queue for that day
                                 (only opened / escalated / reopened)
    out/w1/case_state.json       the persistent case state after the last day

This is the same call `rollup_fast.py` now makes inline; kept separate so the
gate can be re-demonstrated over existing candidate files without re-scanning.
"""
from __future__ import annotations
import json, pathlib
from riskdet.casestore import CaseStore

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
OUT_W1 = ROOT / "out" / "w1"
DISPO_DIR = ROOT / "output" / "dispositions"
DAYS = [f"2026-08-{d:02d}" for d in range(1, 11)]


def _dispositions() -> dict:
    """case_id -> latest recorded human decision (if the dashboard captured any)."""
    out = {}
    if DISPO_DIR.is_dir():
        for p in DISPO_DIR.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            cid = d.get("case_id") or (d.get("event_id", "").split("@")[0])
            if cid and d.get("decision"):
                out[cid] = d["decision"]
    return out


def main() -> None:
    state_path = OUT_W1 / "case_state.json"
    if state_path.is_file():
        state_path.unlink()                          # fresh baseline each replay
    store = CaseStore(state_path)
    dispositions = _dispositions()
    cases_dir = ROOT / "output" / "cases"
    print(f"{'day':<12}{'candidates':>11}{'alerts':>8}{'L4 new':>8}{'L4 pend':>8}   breakdown")
    tot_c = tot_a = 0
    for day in DAYS:
        f = OUT_W1 / f"candidates_{day}.jsonl"
        cands = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        alerts = store.ingest(day, cands, dispositions=dispositions)
        (OUT_W1 / f"alerts_{day}.jsonl").write_text(
            "".join(json.dumps(a, default=str) + "\n" for a in alerts), encoding="utf-8")
        for c in cands:
            if (cases_dir / f"{c['case_id']}.md").is_file():
                store.mark_investigated(c["case_id"], day)
        l4_new = [a for a in alerts if a.get("l4_route")]
        (OUT_W1 / f"l4_queue_{day}.jsonl").write_text(
            "".join(json.dumps(a, default=str) + "\n" for a in l4_new), encoding="utf-8")
        bd = {}
        for a in alerts:
            bd[a["action"]] = bd.get(a["action"], 0) + 1
        tot_c += len(cands); tot_a += len(alerts)
        print(f"{day:<12}{len(cands):>11}{len(alerts):>8}{len(l4_new):>8}"
              f"{len(store.layer4_queue()):>8}   {bd or '-'}")
    store.save()
    hr = sum(1 for s in store.cases.values() if s.max_tier >= 3)
    print(f"\n{'TOTAL':<12}{tot_c:>11}{tot_a:>8}")
    print(f"collapse: {tot_c:,} daily candidate-rows -> {tot_a:,} alerts "
          f"({tot_c / max(tot_a,1):.1f}x fewer)")
    print(f"distinct cases tracked: {len(store.cases):,}  "
          f"(ever reached human_review: {hr})")
    print(f"Layer-4 auto-routed cases: "
          f"{sum(1 for s in store.cases.values() if s.l4_status in ('queued','investigated'))} "
          f"(pending {len(store.layer4_queue())}, investigated "
          f"{sum(1 for s in store.cases.values() if s.l4_status=='investigated')})")


if __name__ == "__main__":
    main()
