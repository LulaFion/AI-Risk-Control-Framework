"""Exception types for the risk-detection package.

Every error carries enough context to act on. A bare RuntimeError in a pipeline
that spends money and reads production player data is not acceptable.
"""

from __future__ import annotations


class RiskdetError(Exception):
    """Base for everything this package raises."""


class Unavailable(RiskdetError):
    """A capability is not usable right now, with a stated reason.

    Borrowed from AI Analysis/statistical_review_board.py: prefer returning an
    explicit 'unavailable, because X' over crashing or silently degrading. The
    caller decides whether that is fatal.
    """


class MissingCredentials(Unavailable):
    """No usable GCP credentials. Message must name how to fix it."""


class CostBudgetExceeded(RiskdetError):
    """A query would scan more bytes than allowed.

    Raised BEFORE the query runs (from the free dry-run estimate), so it costs
    nothing. Carries the numbers so the caller can decide to raise the cap.
    """

    def __init__(self, sql_name: str, estimated_bytes: int, limit_bytes: int,
                 scope: str = "per-query") -> None:
        self.sql_name = sql_name
        self.estimated_bytes = estimated_bytes
        self.limit_bytes = limit_bytes
        self.scope = scope
        gib = 1024 ** 3
        super().__init__(
            f"{sql_name}: estimated {estimated_bytes / gib:,.1f} GiB exceeds the "
            f"{scope} limit of {limit_bytes / gib:,.1f} GiB. "
            f"Raise the limit deliberately or narrow the column list "
            f"(note: a ReportDate filter does NOT reduce bytes on RecordSlot -- "
            f"it is clustered, not date-partitioned)."
        )


class ConfirmationRequired(RiskdetError):
    """Estimate is under the hard cap but over the 'are you sure' threshold."""

    def __init__(self, sql_name: str, estimated_bytes: int, threshold_bytes: int,
                 usd: float) -> None:
        gib = 1024 ** 3
        super().__init__(
            f"{sql_name}: estimated {estimated_bytes / gib:,.1f} GiB "
            f"(~${usd:,.2f}) exceeds the confirmation threshold of "
            f"{threshold_bytes / gib:,.1f} GiB. Pass allow_large=True to proceed."
        )


class ContractError(RiskdetError):
    """An artifact does not match the contract a Layer 4 subagent expects.

    Raised at write time, not read time, so the pipeline fails loudly rather
    than handing an agent a file it will silently misread.
    """


class DataQualityError(RiskdetError):
    """Reference data is unusable. Distinct from 'degraded but usable'."""


class RedactionError(RiskdetError):
    """A secret survived redaction. Always fatal -- never write the file."""
