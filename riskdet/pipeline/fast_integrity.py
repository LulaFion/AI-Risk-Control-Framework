"""Fast, high-cadence INTEGRITY-only scan (the per-minute loop).

Why this exists, and why it is separate from `run`:

  The absolute-integrity rules -- MAX_X_BREACH, BALANCE_IDENTITY, DUP_ROUND --
  are DETERMINISTIC and single-round-true. They need no calibration, no history,
  no peer baseline, and are explicitly exempt from the min_rounds floor. That
  makes them the only rules that can fire meaningfully on a one-minute slice, so
  they are the right (and only) content for a high-cadence scan. Every
  STATISTICAL rule needs volume/history/persistence and stays in the daily `run`.

Mechanics:
  1. Read the last serial watermark (own state file; falls back to the archive
     head on first run so we never scan all of history).
  2. Scan {{source_table}} for `serial > watermark` -- serial leads the source
     clustering, so this prunes to the newest blocks. The dry-run ESTIMATE is an
     upper bound that ignores pruning (like the increment); judge cost by the
     ledger's billed bytes. Runs with allow_large=True.
  3. Build integrity Signals (exact per-(game,play_type) cap from the certified
     sheet is applied here, in Python -- it is not in BigQuery).
  4. fuse() -> candidates (integrity single-family route -> human_review, and
     MAX_X_BREACH -> Critical, come for free from the shared fusion logic).
  5. Feed a DEDICATED case-state gate so per-minute repeats de-duplicate: a
     breach opens a case once, then re-alerts only on a material change. This is
     the same gate the daily scan uses (CLAUDE.md: cadence-agnostic), but a
     separate store so the two cadences never collide.
  6. Advance the watermark (even on a clean scan).

Outputs (under out/, all suffixed _integrity so they never collide with `run`):
  candidates_integrity.jsonl   full machine record of this scan's breaches
  alerts_integrity.jsonl       append-only de-duplicated alert queue (a human)
  case_state_integrity.json    the persistent gate
  integrity_watermark.json     {max_serial, updated}
"""

from __future__ import annotations

import datetime as _dt
import json
import logging

import pandas as pd

from ..bq import CostGuardedBQ
from ..casestore import CaseStore
from ..config import LOG_RETENTION_DAYS, Settings
from ..filters import apply_exclusions
from ..merge import fuse
from ..refdata import GameCatalog, load_catalog
from ..signals import EvidenceRef, Signal
from .ingest import SERIAL_SAFETY_MARGIN, archive_state

log = logging.getLogger("riskdet.fast_integrity")


def _watermark_path(cfg: Settings):
    return cfg.paths.out / "integrity_watermark.json"


def _read_watermark(cfg: Settings) -> int | None:
    p = _watermark_path(cfg)
    if p.is_file():
        try:
            return int(json.loads(p.read_text(encoding="utf-8"))["max_serial"])
        except Exception:
            log.warning("integrity watermark unreadable at %s; ignoring", p)
    return None


def _write_watermark(cfg: Settings, max_serial: int, scan_id: str) -> None:
    p = _watermark_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(
        {"max_serial": int(max_serial), "updated": scan_id},
        indent=2), encoding="utf-8")


def _min_cert_max(catalog: GameCatalog) -> float:
    """Global minimum certified max multiplier -- the SAFE pre-filter floor for
    MAX_X_BREACH: pull any round that could breach the LOWEST cap, then apply the
    exact per-(game, play_type) cap in Python. Never drops a real breach."""
    vals = [r.max_multiplier for r in catalog.rows if r.max_multiplier is not None]
    if not vals:
        log.warning("catalog has no certified max multipliers -- MAX_X_BREACH "
                    "pre-filter disabled (balance/dup still active)")
        return float("inf")
    return float(min(vals))


def _evidence(row, now_utc: _dt.datetime) -> tuple[EvidenceRef, ...]:
    wire = row.get("top_win_seq_ids")
    if not isinstance(wire, str) or not wire:
        return ()
    return tuple(EvidenceRef.parse(w, now_utc=now_utc,
                                   retention_days=LOG_RETENTION_DAYS)
                 for w in wire.split("|") if w)


def _integrity_signals(viol: pd.DataFrame, catalog: GameCatalog,
                       now_utc: _dt.datetime) -> list[Signal]:
    """The three ABSOLUTE integrity rules, identical in shape to layer1.rules."""
    signals: list[Signal] = []

    for _, row in viol[viol.balance_violations > 0].iterrows():
        signals.append(Signal(
            signal_id="L1-BALANCE_IDENTITY", family="INTEGRITY", layer=1,
            grain="player_cell", parent=row.parent, uid=row.uid,
            game_id=row.game_id, play_type=int(row.play_type),
            currency=row.currency, sm_tag=row.sm_tag,
            metric="balance_identity_violations",
            value=float(row.balance_violations), threshold=0.0,
            threshold_method="ABSOLUTE", baseline_source="identity",
            n=int(row.rounds), persistence="not_computable",
            description=(f"{int(row.balance_violations)} round(s) where "
                         f"after != before - bet + win (same-row identity; "
                         f"immune to deposits and session gaps)"),
            evidence=_evidence(row, now_utc)))

    for _, row in viol[viol.dup_seq_rounds > 0].iterrows():
        signals.append(Signal(
            signal_id="L1-DUP_ROUND", family="INTEGRITY", layer=1,
            grain="player_cell", parent=row.parent, uid=row.uid,
            game_id=row.game_id, play_type=int(row.play_type),
            currency=row.currency, sm_tag=row.sm_tag,
            metric="dup_seq_rounds", value=float(row.dup_seq_rounds),
            threshold=0.0, threshold_method="ABSOLUTE",
            baseline_source="identity", n=int(row.rounds),
            persistence="not_computable",
            description=("duplicate game_seq_id settled more than once -- "
                         "double-settle vs ETL duplicate is decidable ONLY by "
                         "the spin-server response count in Cloud Logging"),
            context={"requires_log_adjudication": True},
            evidence=_evidence(row, now_utc)))

    for _, row in viol.iterrows():
        mx, _src = catalog.max_multiplier(row.game_id, int(row.play_type))
        if mx is None or not (float(row.max_multiple) > mx):
            continue
        signals.append(Signal(
            signal_id="L1-MAX_X_BREACH", family="INTEGRITY", layer=1,
            grain="player_cell", parent=row.parent, uid=row.uid,
            game_id=row.game_id, play_type=int(row.play_type),
            currency=row.currency, sm_tag=row.sm_tag,
            metric="max_multiple", value=float(row.max_multiple),
            threshold=float(mx), threshold_method="ABSOLUTE",
            baseline_source="certified", n=int(row.rounds),
            persistence="not_computable",
            description=(f"round paid {float(row.max_multiple):.0f}x vs certified "
                         f"max {mx:.0f}x -- an exploit, a data error, or a wrong "
                         f"sheet; all three are findings"),
            evidence=_evidence(row, now_utc)))

    return signals


def _cand_dict(c) -> dict:
    return {
        "case_id": c.case_id, "grain": c.grain, "parent": c.parent,
        "uid": c.uid, "game_id": c.game_id, "play_type": c.play_type,
        "currency": c.currency, "sm_tag": c.sm_tag,
        "escalation": c.escalation, "severity": c.severity,
        "families": sorted(c.families), "risk_type": "integrity",
        "signals": [s.signal_id for s in c.signals],
        "descriptions": [s.description for s in c.signals],
        "evidence_keys": [e.key for s in c.signals for e in s.evidence],
        "notes": list(c.notes),
    }


def scan_integrity(bq: CostGuardedBQ, *,
                   as_of: _dt.datetime | None = None,
                   since_serial: int | None = None) -> dict:
    """One high-cadence integrity pass. Returns a summary dict."""
    cfg = bq.cfg
    now_utc = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    scan_id = (as_of or now_utc).replace(second=0, microsecond=0).isoformat()

    catalog = load_catalog(cfg.catalog_csv)

    # ---- watermark: own state, else archive head, else 0 ------------------ #
    if since_serial is not None:
        wm = int(since_serial)
    else:
        wm = _read_watermark(cfg)
        if wm is None:
            st = archive_state(bq)
            wm = int(st.max_serial) if st.exists else 0
            log.info("no integrity watermark -- starting at archive head %d", wm)
    # re-read below the watermark by the same safety margin the increment uses,
    # so a DUP_ROUND whose twin landed just before the cut is still paired.
    lookback_wm = max(0, wm - SERIAL_SAFETY_MARGIN)

    params = {"serial_watermark": lookback_wm,
              "min_cert_max": _min_cert_max(catalog)}
    log.info("integrity scan: serial > %d (min_cert_max=%.0f)",
             lookback_wm, params["min_cert_max"])

    df = bq.query_df("60_fast_integrity.sql", params,
                     allow_large=True, stage="integrity-scan")
    if df.empty:  # dry-run-only mode: nothing executed
        return {"scan_id": scan_id, "executed": False,
                "note": "dry_run_only -- no execution"}

    scan_max_serial = int(df.iloc[0]["scan_max_serial"])
    scan_rows = int(df.iloc[0]["scan_rows"])

    # violation cells are the rows where the LEFT JOIN produced a case (parent
    # non-null); a clean scan yields only the summary row.
    viol = df[df["parent"].notna()].copy()
    if not viol.empty:
        viol, _rep = apply_exclusions(viol, cfg)

    signals = _integrity_signals(viol, catalog, now_utc) if not viol.empty else []
    candidates = fuse(cfg, signals) if signals else []

    # ---- de-duplicating case-state gate (dedicated store) ----------------- #
    store = CaseStore(cfg.paths.out / "case_state_integrity.json")
    cand_dicts = [_cand_dict(c) for c in candidates]
    alerts = store.ingest(scan_id, cand_dicts)
    store.save()

    # ---- outputs ---------------------------------------------------------- #
    cfg.paths.out.mkdir(parents=True, exist_ok=True)
    (cfg.paths.out / "candidates_integrity.jsonl").write_text(
        "".join(json.dumps(d, default=str) + "\n" for d in cand_dicts),
        encoding="utf-8")
    if alerts:
        with (cfg.paths.out / "alerts_integrity.jsonl").open(
                "a", encoding="utf-8") as fh:
            for a in alerts:
                fh.write(json.dumps({"scan_id": scan_id, **a}, default=str) + "\n")

    _write_watermark(cfg, scan_max_serial, scan_id)

    by_esc = {e: sum(1 for c in candidates if c.escalation == e)
              for e in ("human_review", "enhanced", "monitor")}
    summary = {
        "scan_id": scan_id, "executed": True,
        "serial_from": lookback_wm, "serial_to": scan_max_serial,
        "rows_scanned": scan_rows,
        "breach_cells": len(candidates),
        "escalation": by_esc,
        "new_alerts": len(alerts),
        "human_review_alerts": sum(1 for a in alerts
                                   if a.get("escalation") == "human_review"),
    }
    log.info("integrity scan %s: %d rows, %d breach cell(s), %d new alert(s) "
             "(%d human_review)", scan_id, scan_rows, len(candidates),
             len(alerts), summary["human_review_alerts"])
    return summary
