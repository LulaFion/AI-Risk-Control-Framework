"""Feature extraction: BigQuery aggregates -> local parquet artifacts.

Raw rounds stay in BigQuery. What comes down is one row per (player, cell),
one row per player (timing), and one row per cell (population constants) --
a few hundred thousand rows total, which is what calibrate.py and Layers 1-3
iterate on locally for free.

Every query here reads through rounds_asof(@as_of_time): the extraction is
replayable at any event-time boundary, which is how August is analysed as if
it were arriving live.
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass
from pathlib import Path

from ..bq import CostGuardedBQ
from ..config import Settings

log = logging.getLogger("riskdet.features")


@dataclass(frozen=True)
class FeatureArtifacts:
    as_of: _dt.datetime
    player_cell: Path
    player_timing: Path
    cell_constants: Path


def _suffix(p: Path, as_of: _dt.datetime) -> Path:
    return p.with_name(f"{p.stem}_{as_of:%Y%m%dT%H%M%S}{p.suffix}")


def extract(bq: CostGuardedBQ, as_of: _dt.datetime,
            *, stage: str = "features",
            min_rounds: int | None = None) -> FeatureArtifacts:
    """Run 20/21/22 at one event-time boundary and persist the aggregates.

    `min_rounds` overrides the player_cell HAVING floor (query 20) ONLY. Pass a
    low value (e.g. 1) to pull an UNGATED player_cell frame so ABSOLUTE integrity
    rules (max-multiple breach, balance identity, duplicate round) can run on
    low-volume cells -- a cap breach on 5 rounds is still a real breach. Because
    the floor is a post-aggregation HAVING, an ungated pull scans the SAME bytes.
    The statistical floor is re-applied in-memory by the caller. Timing (query 21)
    keeps the standard floor regardless.
    """
    cfg: Settings = bq.cfg
    g = cfg.thresholds.get("global", {})
    rules = cfg.thresholds.get("rules", {})
    floor = int(g.get("min_rounds_per_cell", 200))
    pc_floor = floor if min_rounds is None else int(min_rounds)

    cfg.paths.ensure()
    pc = _suffix(cfg.paths.out / "player_cell.parquet", as_of)
    pt = _suffix(cfg.paths.out / "player_timing.parquet", as_of)
    cc = _suffix(cfg.paths.out / "cell_constants.parquet", as_of)

    df = bq.query_df("20_player_cell_features.sql",
                     {"as_of_time": as_of, "min_rounds": pc_floor},
                     stage=stage, allow_large=True)
    df.to_parquet(pc, index=False)
    log.info("player_cell   : %6d rows (floor=%d) -> %s", len(df), pc_floor, pc.name)

    timing_params = {
        "as_of_time": as_of, "min_rounds": floor,
        "session_idle_s": int(g.get("session_idle_minutes", 30)) * 60,
        "gap_cap_s": int(g.get("cadence_gap_cap_seconds", 600)),
        "physical_rpm": int(rules.get("L1-RATE_CEILING", {})
                            .get("physical_rounds_per_minute", 120)),
    }
    df = bq.query_df("21_player_timing.sql", timing_params, stage=stage,
                     allow_large=True)
    df.to_parquet(pt, index=False)
    log.info("player_timing : %6d rows -> %s", len(df), pt.name)

    df = bq.query_df("22_cell_constants.sql", {"as_of_time": as_of}, stage=stage,
                     allow_large=True)
    df.to_parquet(cc, index=False)
    log.info("cell_constants: %6d rows -> %s", len(df), cc.name)

    return FeatureArtifacts(as_of=as_of, player_cell=pc,
                            player_timing=pt, cell_constants=cc)
