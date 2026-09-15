"""Query cost estimation, budgeting, and the audit ledger.

This module replaces the deleted run_detection.ps1 guard. The bq CLI's
--maximum_bytes_billed disappeared with the CLI, so the guarantee is rebuilt
here and made stronger:

    PS1 guard                      -> Python equivalent
    ------------------------------------------------------------------
    bq query --dry_run, parse text -> estimate() reads total_bytes_processed
    print "will scan X GB"         -> logged + ledger row
    if bytes > cap: exit           -> CostBudgetExceeded raised BEFORE the job
    --maximum_bytes_billed         -> QueryJobConfig(maximum_bytes_billed=...)
    (nothing)                      -> RunBudget: cumulative per-process cap
    (nothing)                      -> ledger CSV: every estimate AND actual

Why this matters on this table: acp-develop.OMG.RecordSlot is ~196 GB,
clustered on (serial, game_time) and NOT date-partitioned. Measured on
2026-08-18: a 30-day ReportDate filter and no filter at all both estimate
113,159,972,209 bytes for the calibration column set. Column pruning is the
only lever, so an accidental SELECT * is a real ~$1.20 mistake and a repeated
one is a budget leak. Every query goes through this module first.
"""

from __future__ import annotations

import csv
import datetime as _dt
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import CostBudgetExceeded

GIB = 1024 ** 3
TIB = 1024 ** 4


def human(nbytes: int | None) -> str:
    """'126.8 GiB' -- for logs and ledger rows."""
    if nbytes is None:
        return "?"
    if nbytes >= TIB:
        return f"{nbytes / TIB:,.2f} TiB"
    if nbytes >= GIB:
        return f"{nbytes / GIB:,.1f} GiB"
    return f"{nbytes / (1024 ** 2):,.1f} MiB"


@dataclass(frozen=True)
class CostEstimate:
    """Result of a free dry run."""

    sql_name: str
    bytes_processed: int
    usd: float
    cache_hit: bool
    referenced_tables: tuple[str, ...] = ()

    def __str__(self) -> str:
        return (f"{self.sql_name}: ~{human(self.bytes_processed)} "
                f"(~${self.usd:,.2f})")


@dataclass
class RunBudget:
    """Cumulative bytes cap for one process.

    The PS1 wrapper capped each query; nothing capped a whole run, so a loop
    of 'small' queries could still spend freely. reserve() is called with the
    dry-run estimate before each real job; when the sum would cross the limit,
    the job never starts.
    """

    limit_bytes: int
    spent_bytes: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def reserve(self, nbytes: int, sql_name: str) -> None:
        with self._lock:
            if self.spent_bytes + nbytes > self.limit_bytes:
                raise CostBudgetExceeded(
                    sql_name, self.spent_bytes + nbytes, self.limit_bytes,
                    scope="run-cumulative")
            self.spent_bytes += nbytes

    def refund(self, nbytes: int) -> None:
        """Return unspent reservation (e.g. dry_run_only mode, or actual <
        estimate). Never lets spent go negative."""
        with self._lock:
            self.spent_bytes = max(0, self.spent_bytes - nbytes)

    def remaining(self) -> int:
        with self._lock:
            return max(0, self.limit_bytes - self.spent_bytes)


_LEDGER_FIELDS = [
    "ts_utc", "sql_name", "job_id", "status",
    "estimated_bytes", "billed_bytes", "estimated_usd", "billed_usd",
    "cache_hit", "identity", "note",
]


def append_ledger(path: Path, *, sql_name: str, status: str,
                  estimate: CostEstimate | None = None,
                  billed_bytes: int | None = None,
                  usd_per_tib: float = 6.25,
                  job_id: str | None = None,
                  identity: str = "",
                  note: str = "") -> None:
    """Append one row to the audit ledger.

    The ledger is the reconciliation surface: estimates vs actuals, per query,
    per identity. It lives under output/data_quality/ because report-composer
    treats that directory as the data-caveat source for the daily digest.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    row = {
        "ts_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sql_name": sql_name,
        "job_id": job_id or "",
        "status": status,
        "estimated_bytes": estimate.bytes_processed if estimate else "",
        "billed_bytes": billed_bytes if billed_bytes is not None else "",
        "estimated_usd": f"{estimate.usd:.4f}" if estimate else "",
        "billed_usd": (f"{(billed_bytes / TIB) * usd_per_tib:.4f}"
                       if billed_bytes is not None else ""),
        "cache_hit": estimate.cache_hit if estimate else "",
        "identity": identity,
        "note": note,
    }
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_LEDGER_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
