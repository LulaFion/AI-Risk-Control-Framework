"""The redaction boundary is the whole safety case for alert delivery: Telegram
is a third-party service and these are real player/risk records. These tests
assert that nothing but the allow-listed fields (plus the deliberately-accepted
case identifier) can ever leave GCP.

The reflective test at the bottom is the important one -- it fails the build if
someone adds a key to CaseStore.context without consciously classifying it.
"""
import datetime as _dt
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import riskdet  # noqa: E402
from riskdet import casestore as CS  # noqa: E402
from riskdet.notify.config import notify_settings  # noqa: E402
from riskdet.notify.payload import (ALLOWED_KEYS, FORBIDDEN_SOURCE_KEYS,  # noqa: E402
                                    safe_payload)

SCAN = "2026-09-14T08:41:00"

# An alert exactly as CaseStore.ingest() emits it (casestore.py:216-220), with
# extra sensitive keys a future change might add.
ALERT = {
    "case_id": "P_jdbkxmmk_1049s_kx4iq1ui2xsn",
    "action": "opened",
    "scan": SCAN,
    "escalation": "human_review",
    "severity": "Critical",
    "families": ["INTEGRITY", "OUTCOME_MAGNITUDE"],
    "note": "opened at human_review/Critical",
    "l4_route": True,
    # --- context (casestore.py:157-159) ---
    "uid": "kx4iq1ui2xsn",
    "parent": "jdbkxmmk",
    "game_id": "1049s",
    "currency": "MMK",
    "risk_type": "Advantage Play",
    # --- hypothetical future leaks ---
    "email": "player@example.com",
    "amount": "918273.55",
    "balance": "33897.73",
}


def _cfg(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("RISKDET_DASHBOARD_URL", "https://dash.example.run.app")
    return riskdet.settings()


def test_only_allowlisted_keys_survive(monkeypatch):
    cfg = _cfg(monkeypatch)
    ncfg = notify_settings(cfg, source="integrity")
    p = safe_payload(ALERT, ncfg, scan_id=SCAN, source="integrity")

    assert p is not None
    derived = {"sla_label", "sla_due_utc", "source", "link"}
    assert set(p) <= set(ALLOWED_KEYS) | derived


def test_forbidden_values_never_appear_outside_the_case_id(monkeypatch):
    """Value-level, not key-level: a uid smuggled into any non-id field fails."""
    cfg = _cfg(monkeypatch)
    ncfg = notify_settings(cfg, source="daily")
    p = safe_payload(ALERT, ncfg, scan_id=SCAN, source="daily")
    assert p is not None

    # case_id and link legitimately embed the identifier (accepted decision).
    scanned = json.dumps({k: v for k, v in p.items()
                          if k not in ("case_id", "link")},
                         default=str).lower()
    for key in ("email", "amount", "balance", "risk_type", "currency"):
        assert str(ALERT[key]).lower() not in scanned, f"{key} leaked"


def test_note_excluded_by_default_and_optin(monkeypatch):
    cfg = _cfg(monkeypatch)
    assert "note" not in safe_payload(
        ALERT, notify_settings(cfg), scan_id=SCAN, source="daily")

    monkeypatch.setenv("RISKDET_NOTIFY_INCLUDE_NOTE", "1")
    p = safe_payload(ALERT, notify_settings(cfg), scan_id=SCAN, source="daily")
    assert p["note"] == ALERT["note"]


def test_guard_drops_the_message_when_a_value_leaks(monkeypatch):
    """A uid that ends up in a family label must DROP the alert, not send it."""
    cfg = _cfg(monkeypatch)
    ncfg = notify_settings(cfg)
    bad = {**ALERT, "families": ["INTEGRITY", "kx4iq1ui2xsn"]}
    assert safe_payload(bad, ncfg, scan_id=SCAN, source="daily") is None


def test_opaque_ids_hide_the_identifier(monkeypatch):
    cfg = _cfg(monkeypatch, RISKDET_NOTIFY_OPAQUE_IDS="1",
               RISKDET_REDACT_SALT="pinned-salt")
    ncfg = notify_settings(cfg)
    p = safe_payload(ALERT, ncfg, scan_id=SCAN, source="daily")

    assert p["case_id"].startswith("c_")
    blob = json.dumps(p).lower()
    assert "kx4iq1ui2xsn" not in blob and "jdbkxmmk" not in blob
    # stable across calls, so the dashboard can resolve it
    assert p["case_id"] == safe_payload(ALERT, ncfg, scan_id=SCAN,
                                        source="daily")["case_id"]


def test_sla_matches_the_documented_policy(monkeypatch):
    cfg = _cfg(monkeypatch)
    ncfg = notify_settings(cfg)
    got = {sev: safe_payload({**ALERT, "severity": sev}, ncfg,
                             scan_id=SCAN, source="daily")["sla_label"]
           for sev in ("Critical", "High", "Medium", "Low")}
    assert got == {"Critical": "1h", "High": "same-day",
                   "Medium": "48h", "Low": "weekly"}

    crit = safe_payload(ALERT, ncfg, scan_id=SCAN, source="daily")
    assert crit["sla_due_utc"] == "2026-09-14T09:41Z"      # scan + 1h


def test_missing_case_id_is_dropped(monkeypatch):
    cfg = _cfg(monkeypatch)
    ncfg = notify_settings(cfg)
    assert safe_payload({k: v for k, v in ALERT.items() if k != "case_id"},
                        ncfg, scan_id=SCAN, source="daily") is None


def test_no_credentials_forces_dry_run(monkeypatch):
    """A developer running a scan locally must never be able to page the team."""
    monkeypatch.delenv("RISKDET_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("RISKDET_TELEGRAM_CHAT_ID", raising=False)
    assert notify_settings(riskdet.settings()).dry_run is True


@pytest.mark.parametrize("ctx_key", ("uid", "parent", "game_id", "currency",
                                     "risk_type"))
def test_every_casestore_context_key_is_classified(monkeypatch, ctx_key):
    """REGRESSION GUARD. CaseStore merges its context into every alert. Each of
    those keys must be either allow-listed (a conscious decision) or on the
    forbidden list -- never silently unclassified."""
    assert ctx_key in ALLOWED_KEYS or ctx_key in FORBIDDEN_SOURCE_KEYS, (
        f"CaseStore.context key {ctx_key!r} is neither allow-listed nor "
        f"forbidden -- classify it in riskdet/notify/payload.py before it "
        f"can reach a third-party service")
