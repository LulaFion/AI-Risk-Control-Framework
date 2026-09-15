"""Alert delivery -- push a Critical/High case to a human within seconds.

Detection is worthless if nobody is told: the case-state gate already produces a
de-duplicated, actionable alert list, and until this package existed it was only
ever written to a JSONL file in a bucket.

Boundaries this package holds:
  * It NEVER raises into a scan. Delivery is best-effort on top of a durable
    record that is always written first.
  * It NEVER sends player data. `payload.safe_payload()` is the single
    allow-listed boundary; everything else stays behind the dashboard.
  * It NEVER decides anything. It notifies a human, who decides. Enforcement
    remains 100% human, exactly as elsewhere in this system.
"""
from .config import NotifyConfig, notify_settings  # noqa: F401
from .payload import safe_payload  # noqa: F401

__all__ = ["NotifyConfig", "notify_settings", "safe_payload", "deliver"]


def __getattr__(name: str):
    """Lazy re-export so importing the package costs nothing until delivery is
    actually used (the scan hot path imports this module on every run)."""
    if name == "deliver":
        from .deliver import deliver  # noqa: PLC0415
        return deliver
    raise AttributeError(name)
