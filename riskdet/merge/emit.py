"""Artifact emission -- the contract surface the Layer 4 subagents read.

Python writes exactly four things; the agents write everything else:

  out/candidates.jsonl              full-fidelity machine record (reproducible)
  output/case_queue.md              the roster player-investigator/composer read
  output/risk_rules_findings.csv    flat findings; carries window_start/end so
                                    report-composer can detect staleness
  output/cases/, output/reports/    directories only -- agents own the files

Discipline carried from the agent contracts: evidence strings are copied
VERBATIM from the signal engine (agents quote them, never recompute); a rule
match is a candidate, never a finding; game-level cases are separate rows from
player cases and never merged.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as _dt
import json
import logging
from pathlib import Path

from ..config import Settings
from .fuse import Candidate

log = logging.getLogger("riskdet.emit")


def _sig_dict(s) -> dict:
    d = dataclasses.asdict(s)
    d["evidence"] = [e.key for e in s.evidence]
    return d


def emit(cfg: Settings, candidates: list[Candidate],
         *, window_start: _dt.datetime, window_end: _dt.datetime,
         dq_verdicts: dict[str, str] | None = None) -> dict[str, Path]:
    paths = cfg.paths.ensure()
    written: dict[str, Path] = {}

    # ---- out/candidates.jsonl -------------------------------------------- #
    with paths.candidates_jsonl.open("w", encoding="utf-8") as fh:
        for c in candidates:
            fh.write(json.dumps({
                "case_id": c.case_id, "grain": c.grain,
                "parent": c.parent, "uid": c.uid, "game_id": c.game_id,
                "play_type": c.play_type, "currency": c.currency,
                "sm_tag": c.sm_tag,
                "escalation": c.escalation, "severity": c.severity,
                "independent_families": c.independent_families,
                "families": sorted(c.families),
                "cohort_id": c.cohort_id,
                "expected_max_z": c.expected_max_z,
                "notes": c.notes,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "last_active_utc": c.last_active_utc,
                "latest_evidence_utc": c.latest_evidence_utc,
                "evidence_keys": c.evidence_keys,
                "signals": [_sig_dict(s) for s in c.signals],
            }, ensure_ascii=False, default=str) + "\n")
    written["candidates_jsonl"] = paths.candidates_jsonl

    # ---- output/risk_rules_findings.csv ----------------------------------- #
    with paths.findings_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "grain", "parent", "uid", "game_id",
                    "play_type", "currency", "sm_tag", "escalation",
                    "severity", "independent_families", "families",
                    "signal_id", "family", "metric", "value", "threshold",
                    "threshold_method", "baseline_source", "p_value",
                    "effect", "effect_unit", "n", "persistence",
                    "cohort_id", "description", "evidence_keys",
                    "window_start", "window_end"])
        for c in candidates:
            for s in c.signals:
                w.writerow([
                    c.case_id, c.grain, c.parent or "", c.uid or "",
                    s.game_id or "", "" if s.play_type is None else s.play_type,
                    s.currency or "", s.sm_tag or "", c.escalation,
                    c.severity, c.independent_families,
                    "|".join(sorted(c.families)),
                    s.signal_id, s.family, s.metric,
                    f"{s.value:.6g}",
                    "" if s.threshold is None else f"{s.threshold:.6g}",
                    s.threshold_method, s.baseline_source,
                    "" if s.p_value is None else f"{s.p_value:.3e}",
                    "" if s.effect is None else f"{s.effect:.4g}",
                    s.effect_unit, s.n or "", s.persistence,
                    c.cohort_id or "", s.description,
                    "|".join(e.key for e in s.evidence),
                    window_start.isoformat(), window_end.isoformat()])
    written["findings_csv"] = paths.findings_csv

    # ---- output/case_queue.md --------------------------------------------- #
    lines = [
        "# Case queue",
        "",
        f"- window (UTC event time): `{window_start.isoformat()}` .. "
        f"`{window_end.isoformat()}`",
        f"- candidates: {len(candidates)} "
        f"({sum(1 for c in candidates if c.escalation == 'human_review')} for "
        f"human review, "
        f"{sum(1 for c in candidates if c.escalation == 'enhanced')} enhanced "
        f"monitoring, "
        f"{sum(1 for c in candidates if c.escalation == 'monitor')} monitor)",
        "",
        "> A rule match is a REASON TO INVESTIGATE, never a finding. Findings "
        "exist only after investigation and skeptic review.",
        "",
    ]
    if dq_verdicts:
        warn = {k: v for k, v in dq_verdicts.items() if v != "PASS"}
        if warn:
            lines += ["## Data caveats (read first)", ""]
            lines += [f"- `{k}`: **{v}** -- see output/data_quality/"
                      for k, v in warn.items()]
            lines += [""]

    def _rows(cands: list[Candidate], owner: str) -> list[str]:
        rows = ["| case | escalation | severity | families | signals | "
                "persistence | cohort | evidence keys |",
                "|---|---|---|---|---|---|---|---|"]
        for c in cands:
            sigs = "; ".join(sorted({s.signal_id for s in c.signals}))
            persist = "; ".join(sorted({s.persistence for s in c.signals}))
            ev = "<br>".join(c.evidence_keys[:4]) or "-"
            rows.append(
                f"| `{c.case_id}` | {c.escalation} | {c.severity} | "
                f"{c.independent_families} ({', '.join(sorted(c.families))}) | "
                f"{sigs} | {persist} | {c.cohort_id or '-'} | {ev} |")
        rows += ["", f"Suggested owner: `{owner}`", ""]
        return rows

    players = [c for c in candidates if c.grain == "player"]
    games = [c for c in candidates if c.grain == "game_cell"]
    if players:
        lines += ["## Player cases", ""] + _rows(players, "player-investigator")
        noted = [c for c in players if c.notes]
        if noted:
            lines += ["### Context notes", ""]
            for c in noted:
                for n in c.notes:
                    lines.append(f"- `{c.case_id}`: {n}")
            lines += [""]
    if games:
        lines += ["## Game-level cases (never merged into player cases)",
                  ""] + _rows(games, "query-analyst")
    paths.case_queue.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written["case_queue"] = paths.case_queue

    log.info("emitted %d candidates -> %s", len(candidates),
             ", ".join(p.name for p in written.values()))
    return written
