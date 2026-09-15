"""Archive lifecycle: one-time bootstrap, idempotent increments, replay gate.

The archive (`OMG_riskdet.rounds_all`) is the only copy of the rounds this
package ever makes, and it never leaves BigQuery -- aggregates come down,
raw rounds do not.

    bootstrap()   ONE-TIME. Scans the unpartitioned source once (~109 GiB) and
                  materialises the full May-August history, partitioned by UTC
                  event date, clustered (parent, uid). Refuses to run if the
                  archive already exists -- there is no re-run path by design;
                  September and later arrive via increment().
    increment()   Idempotent MERGE. Appends new rounds / updates late-arriving
                  corrections. Historical partitions are never deleted.
    ensure_replay_gate()
                  (Re)creates the rounds_asof(as_of DATETIME) table function --
                  the structural leakage gate every analytical query reads
                  through.
    replay_ticks() Yields the as_of boundaries that simulate hourly/daily data
                  arrival across the hidden holdout.
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass
from typing import Iterator

from ..bq import CostGuardedBQ
from ..config import Settings, settings
from ..errors import DataQualityError

log = logging.getLogger("riskdet.ingest")

# How far below the serial watermark the increment re-reads, to catch rows
# committed out of serial order around the previous run's cut.
SERIAL_SAFETY_MARGIN = 100_000
# How far back last_modify_time looks for corrections to already-ingested rows.
MODIFIED_LOOKBACK_DAYS = 3


@dataclass(frozen=True)
class ArchiveState:
    exists: bool
    num_rows: int = 0
    max_serial: int = 0
    max_game_time: _dt.datetime | None = None


def archive_state(bq: CostGuardedBQ) -> ArchiveState:
    table = bq.cfg.work_table_rounds
    try:
        rows = bq.table_num_rows(table)
    except Exception:
        return ArchiveState(exists=False)
    # watermarks: tiny scan of two columns of the (partitioned) archive
    df = bq.query_df("08_watermarks.sql", stage="ingest")
    if df.empty:  # dry_run_only mode
        return ArchiveState(exists=True, num_rows=rows)
    r = df.iloc[0]
    return ArchiveState(
        exists=True, num_rows=rows,
        max_serial=int(r["max_serial"]),
        max_game_time=r["max_game_time"].to_pydatetime()
        if hasattr(r["max_game_time"], "to_pydatetime") else r["max_game_time"])


def bootstrap(bq: CostGuardedBQ) -> None:
    """The one paid scan of the source. Guarded twice against re-running."""
    state = archive_state(bq)
    if state.exists and state.num_rows > 0:
        raise DataQualityError(
            f"archive {bq.cfg.work_table_rounds} already exists with "
            f"{state.num_rows:,} rows -- bootstrap is one-time only. "
            f"New data arrives via increment(); this is what preserves "
            f"historical partitions.")
    bq.ensure_dataset()
    # CREATE TABLE IF NOT EXISTS in the SQL is the second guard.
    bq.query_df("05_bootstrap_archive.sql", allow_large=True, stage="bootstrap")
    log.info("bootstrap complete: %s", bq.cfg.work_table_rounds)


def increment(bq: CostGuardedBQ) -> None:
    """Idempotent MERGE of new + late-arriving rounds.

    The dry-run estimate for this will look like a near-full source scan --
    estimates report an UPPER BOUND that ignores cluster pruning on serial.
    Judge by the ledger's billed bytes after the first real run: if billed
    approaches the full scan, clustering is not helping and the documented
    fallback is a monthly full MERGE at ~$0.70.
    """
    state = archive_state(bq)
    if not state.exists or state.num_rows == 0:
        raise DataQualityError("archive missing -- run bootstrap first")
    if state.max_game_time is None:
        raise DataQualityError("archive watermarks unavailable (dry-run mode?)")
    params = {
        "serial_watermark": max(0, state.max_serial - SERIAL_SAFETY_MARGIN),
        "modified_since": (state.max_game_time
                           - _dt.timedelta(days=MODIFIED_LOOKBACK_DAYS)),
    }
    log.info("increment: serial > %(serial_watermark)s or modified >= "
             "%(modified_since)s", params)
    bq.query_df("06_ingest_increment.sql", params,
                allow_large=True, stage="increment")


def ensure_replay_gate(bq: CostGuardedBQ) -> None:
    """Create/refresh the rounds_asof TVF. DDL only -- scans nothing."""
    bq.query_df("07_replay_tvf.sql", stage="replay-gate")


def train_end(cfg: Settings) -> _dt.datetime:
    return _dt.datetime.fromisoformat(cfg.thresholds["windows"]["train_end"])


def validation_end(cfg: Settings) -> _dt.datetime:
    return _dt.datetime.fromisoformat(cfg.thresholds["windows"]["validation_end"])


def replay_ticks(cfg: Settings | None = None,
                 start: _dt.datetime | None = None,
                 end: _dt.datetime | None = None,
                 step_hours: int | None = None) -> Iterator[_dt.datetime]:
    """as_of boundaries simulating periodic arrival across the holdout.

    Default: from validation_end forward in `replay_step_hours` steps. Each
    tick is passed as @as_of_time to rounds_asof -- the archive is scanned
    with partition pruning; the unpartitioned source is never touched.
    """
    cfg = cfg or settings()
    w = cfg.thresholds["windows"]
    t = start or _dt.datetime.fromisoformat(w["validation_end"])
    stop = end or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    step = _dt.timedelta(hours=step_hours or int(w.get("replay_step_hours", 24)))
    while t <= stop:
        yield t
        t += step
