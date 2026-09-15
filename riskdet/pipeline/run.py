"""Orchestrators: calibration cycle and detection scan.

calibrate_cycle()  extract features at train_end and validation_end, fit on
                   the training window, FREEZE, validate on June-July, persist
                   the artifacts. Paid: ~2 feature extractions.
scan()             extract features at as_of, evaluate the FROZEN rules
                   (Layers 1-3), fuse, emit the Layer-4 artifact surface.
                   as_of is REQUIRED -- it is the leakage gate.

Both refuse to fabricate: no calibration artifact -> scan() raises rather than
guessing thresholds; empty windows emit an empty queue rather than padding.
"""

from __future__ import annotations

import datetime as _dt
import logging

import pandas as pd

from .. import calibrate as C
from ..bq import CostGuardedBQ
from ..config import Settings
from ..layer1 import run_layer1
from ..layer2 import run_layer2
from ..layer3 import detect_cohorts, membership, run_ml_discovery, write_proposals
from ..merge import emit, fuse
from ..refdata import load_catalog
from .features import FeatureArtifacts, extract
from .ingest import train_end, validation_end

log = logging.getLogger("riskdet.run")


def _load(arts: FeatureArtifacts) -> tuple[pd.DataFrame, ...]:
    return (pd.read_parquet(arts.player_cell),
            pd.read_parquet(arts.player_timing),
            pd.read_parquet(arts.cell_constants))


def _cell_operator(bq: CostGuardedBQ, as_of: _dt.datetime) -> pd.DataFrame:
    return bq.query_df("23_cell_operator_constants.sql",
                       {"as_of_time": as_of}, stage="features", allow_large=True)


def calibrate_cycle(bq: CostGuardedBQ) -> str:
    cfg = bq.cfg
    catalog = load_catalog(cfg.catalog_csv)
    catalog.write_dq_report(cfg.paths.dq_report("catalog", _dt.date.today()))
    t_end, v_end = train_end(cfg), validation_end(cfg)

    log.info("calibrate: extracting training window (as_of=%s)", t_end)
    pc, pt, cc = _load(extract(bq, t_end, stage="calibrate-train"))
    co = _cell_operator(bq, t_end)
    if pc.empty:
        raise RuntimeError("training extraction returned no rows "
                           "(dry-run mode? empty archive?)")
    frozen = C.fit(cfg, C.build_metrics(cfg, catalog, pc, pt, cc, co), t_end)

    log.info("calibrate: validating on frozen params (as_of=%s)", v_end)
    vpc, vpt, vcc = _load(extract(bq, v_end, stage="calibrate-validate"))
    vco = _cell_operator(bq, v_end)
    val = C.validate(cfg, frozen,
                     C.build_metrics(cfg, catalog, vpc, vpt, vcc, vco), v_end)
    C.write_artifacts(cfg, frozen, val)
    log.info("calibration verdict: %s", val.verdict)
    return val.verdict


def scan(bq: CostGuardedBQ, as_of: _dt.datetime,
         *, persistence_mid: _dt.datetime | None = None) -> dict:
    cfg = bq.cfg
    catalog = load_catalog(cfg.catalog_csv)
    frozen = C.load_frozen(cfg)

    # Pull the player_cell frame UNGATED (min_rounds=1). Statistical rules need
    # volume, so they run on the floor-gated subset below; ABSOLUTE integrity
    # rules (max-multiple breach, balance identity, duplicate round) run on the
    # full frame -- a cap breach on a low-volume cell is still a real breach, and
    # gating it out was a production miss. The floor is a post-aggregation HAVING,
    # so the ungated pull scans the same bytes.
    pc_full, pt, cc = _load(extract(bq, as_of, stage="scan", min_rounds=1))
    co = _cell_operator(bq, as_of)
    if pc_full.empty:
        log.warning("scan window empty -- emitting empty queue")
        emit(cfg, [], window_start=as_of, window_end=as_of)
        return {"candidates": 0, "note": "empty window"}
    floor = int(cfg.thresholds["global"]["min_rounds_per_cell"])
    pc = pc_full[pc_full.rounds >= floor].copy()   # statistical population

    half_pc = half_pt = None
    if persistence_mid is not None:
        half_pc, half_pt, _ = _load(
            extract(bq, persistence_mid, stage="scan-mid"))

    metrics = C.build_metrics(cfg, catalog, pc, pt, cc, co)
    sigs = run_layer1(cfg, catalog, frozen, metrics, pc, pt, cc,
                      integrity_pc=pc_full,
                      half_player_cell=half_pc, half_player_timing=half_pt)

    game_day = bq.query_df("30_game_day_series.sql",
                           {"as_of_time": as_of}, stage="scan", allow_large=True)
    if not game_day.empty:
        sigs += run_layer2(cfg, catalog, game_day)

    proposals = detect_cohorts(cfg, catalog, pc, pt, cc)
    if proposals:
        write_proposals(cfg, proposals)
    rule_flagged = {(s.parent, s.uid) for s in sigs
                    if s.parent is not None and s.uid is not None}
    ml_sigs, ml_val = run_ml_discovery(cfg, pc, pt, rule_flagged)
    sigs += ml_sigs

    n_players = int(pc.groupby(["parent", "uid"]).ngroups)
    cands = fuse(cfg, sigs, cohort_membership=membership(proposals),
                 population_tested=n_players)

    # --- recency: latest SUSPICIOUS evidence + last activity -----------------
    # latest_evidence_utc = newest evidence-round timestamp (game_seq_id@<ts>) =
    # the day the risk actually shows. last_active_utc = when the player last
    # played (max last_round_utc). A case is DATED and FILTERED by its evidence
    # recency (preferred), so a June breach is a June event, never surfaced as a
    # September event.
    def _latest_ev(c) -> str | None:
        latest = ""
        for k in c.evidence_keys:
            _, _, ts = str(k).partition("@")
            if ts and ts > latest:
                latest = ts
        return latest or None
    last_active = {}
    if not pc_full.empty and "last_round_utc" in pc_full.columns:
        la = pc_full.groupby(["parent", "uid"])["last_round_utc"].max()
        last_active = {k: pd.to_datetime(v).strftime("%Y-%m-%dT%H:%M:%SZ")
                       for k, v in la.items() if pd.notna(v)}
    for c in cands:
        c.latest_evidence_utc = _latest_ev(c)
        if c.parent is not None and c.uid is not None:
            c.last_active_utc = last_active.get((c.parent, c.uid))

    # --- rolling DAILY detection window (config: windows.detection_window_days)
    # 0/absent => cumulative (old behaviour). >0 => a case passes only if its
    # SUSPICIOUS EVIDENCE (preferred) -- or, absent dated evidence, its player
    # activity -- falls within N days of as_of. So a daily scan surfaces only
    # THAT day's risk; historical breaches (old evidence) are excluded as today's
    # events but still inform baselines. Game-level cases without recency are kept.
    det_days = int((cfg.thresholds.get("windows") or {}).get("detection_window_days", 0) or 0)
    if det_days > 0:
        cutoff = as_of - _dt.timedelta(days=det_days)
        def _recent(c) -> bool:
            r = c.latest_evidence_utc or c.last_active_utc
            if not r:
                return c.uid is None            # game-level w/o recency: keep
            try:
                return _dt.datetime.strptime(r, "%Y-%m-%dT%H:%M:%SZ") >= cutoff
            except ValueError:
                return True
        before = len(cands)
        cands = [c for c in cands if _recent(c)]
        log.info("daily window %dd (cutoff %s): kept %d/%d candidates",
                 det_days, cutoff.date(), len(cands), before)

    window_start = pd.to_datetime(pc_full.first_round_utc.min()).to_pydatetime()
    emit(cfg, cands, window_start=window_start, window_end=as_of,
         dq_verdicts={"catalog": catalog.verdict,
                      "ml_validation": ml_val.verdict})
    return {
        "candidates": len(cands),
        "human_review": sum(1 for c in cands if c.escalation == "human_review"),
        "signals": len(sigs),
        "cohort_proposals": len(proposals),
        "ml_validation": ml_val.verdict,
        "population": n_players,
    }
