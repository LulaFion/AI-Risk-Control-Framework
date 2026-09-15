"""riskdet -- AI Risk Detector for slot gameplay on GCP.

Layers 1-3 (rules, statistics, ML) are implemented here in Python. Layer 4 is
the Claude Code subagent team in .claude/agents/; this package produces the
artifacts those agents consume and never makes an enforcement decision itself.

Portability contract: no subprocess, no bq/gcloud CLI, no OS-specific paths.
Everything goes through google-cloud-* client libraries and pathlib so the same
code runs on Windows locally and in a Linux container.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .config import Settings, check, settings  # noqa: F401
from .errors import (  # noqa: F401
    ConfirmationRequired,
    ContractError,
    CostBudgetExceeded,
    DataQualityError,
    MissingCredentials,
    RedactionError,
    RiskdetError,
    Unavailable,
)

__all__ = [
    "__version__",
    "Settings",
    "settings",
    "check",
    "RiskdetError",
    "Unavailable",
    "MissingCredentials",
    "CostBudgetExceeded",
    "ConfirmationRequired",
    "ContractError",
    "DataQualityError",
    "RedactionError",
]
