"""Layer 3 -- coordinated cohort detection and unsupervised ML discovery."""

from .cohorts import (  # noqa: F401
    COMPETING_EXPLANATIONS,
    CohortProposal,
    detect_cohorts,
    membership,
    write_proposals,
)
from .ml import MLValidation, run_ml_discovery  # noqa: F401

__all__ = ["CohortProposal", "detect_cohorts", "membership", "write_proposals",
           "COMPETING_EXPLANATIONS", "MLValidation", "run_ml_discovery"]
