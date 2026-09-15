"""Provider selection — swap the backend without touching the UI.

DASHBOARD_PROVIDER=riskdet (default) -> RiskdetProvider: real detection output
                                        (out/candidates.jsonl, output/cases/*.md,
                                        output/reports/, output/data_quality/).
DASHBOARD_PROVIDER=mock              -> MockProvider (deterministic demo data),
                                        kept only for UI development.
"""

from __future__ import annotations

import os

from .base import RISK_TYPES, SEVERITIES, STATUSES, DataProvider  # noqa: F401


def get_provider() -> DataProvider:
    kind = os.environ.get("DASHBOARD_PROVIDER", "riskdet").lower()
    if kind == "riskdet":
        from .riskdet_live import RiskdetProvider
        return RiskdetProvider()
    if kind == "mock":
        from .mock import MockProvider
        return MockProvider()
    raise NotImplementedError(
        f"unknown DASHBOARD_PROVIDER '{kind}' (use 'riskdet' or 'mock')")
