"""The ONLY place an alert becomes something that leaves GCP.

Telegram is a third-party service and these are real player/risk records, so the
outbound record is built from an **allow-list, never a deny-list**. `CaseStore`
already merges its context dict into every alert
(`riskdet/casestore.py:216-220` -> uid, parent, game_id, currency, risk_type);
if someone adds a key there tomorrow, an allow-list is inert while a deny-list
would leak it silently.

ONE deliberate exception, decided by the operator: `case_id` is
`P_{parent}_{game_id}_{uid}` (`riskdet/paths.py:45-46`), so the identifier itself
carries the operator and uid. Sending it raw was chosen for operability -- a
reviewer must recognise the case at a glance, and the deep link is useless
without it. `RISKDET_NOTIFY_OPAQUE_IDS=1` swaps in a salted digest for
deployments where that is not acceptable.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
from typing import Any
from urllib.parse import quote

from .config import NotifyConfig, sla_for

log = logging.getLogger("riskdet.notify")

# Everything else in the alert dict is dropped.
ALLOWED_KEYS = ("case_id", "action", "escalation", "severity", "families",
                "scan", "l4_route")

# Source keys whose VALUES must never appear in the outbound record. Used for
# the value-level guard below -- the key-level allow-list already excludes them.
#
# `game_id` is listed deliberately: it is not player-identifying (every player
# of that game shares it), but the operator chose "identifier + link only", so
# it is not emitted as its own field either. Classifying it here keeps the
# reflective regression test in tests/test_notify_payload.py honest.
FORBIDDEN_SOURCE_KEYS = ("uid", "parent", "operator", "game_id", "currency",
                         "risk_type", "amount", "bet", "win", "balance",
                         "turnover", "evidence_keys", "signals",
                         "descriptions", "email")

# Fields permitted to embed the case identifier (and therefore, by the decision
# above, the operator/uid it is built from). Excluded from the value scan.
_ID_BEARING = ("case_id", "link")

_MIN_LEAK_TOKEN = 3       # ignore 1-2 char values; too collision-prone to test


def _opaque(case_id: str, salt: str | None) -> str:
    """Stable surrogate id. Requires a PINNED RISKDET_REDACT_SALT -- the salt is
    random per run when unset (riskdet/config.py:236), which would make the id
    change every scan and break dashboard resolution."""
    h = hashlib.sha256(f"{salt or ''}|{case_id}".encode("utf-8"))
    return f"c_{h.hexdigest()[:12]}"


def _parse_scan(scan: Any) -> _dt.datetime:
    """Scan ids are ISO strings (daily: as_of; integrity: minute resolution)."""
    if isinstance(scan, _dt.datetime):
        return scan
    try:
        return _dt.datetime.fromisoformat(str(scan).replace("Z", ""))
    except (TypeError, ValueError):
        return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _assert_no_leak(out: dict, alert: dict) -> bool:
    """Value-level guard: no forbidden SOURCE value may appear anywhere in the
    outbound record, except inside the id-bearing fields.

    Key-level filtering alone is not enough -- a uid embedded in a free-text
    note, or an operator name that found its way into a family label, would pass
    an allow-list on keys but still leave the perimeter.
    """
    scanned = json.dumps({k: v for k, v in out.items() if k not in _ID_BEARING},
                         default=str, ensure_ascii=False).lower()
    for key in FORBIDDEN_SOURCE_KEYS:
        raw = alert.get(key)
        if raw is None:
            continue
        val = str(raw).strip().lower()
        if len(val) >= _MIN_LEAK_TOKEN and val in scanned:
            log.error("notify: payload guard tripped on %r -- dropping alert %s",
                      key, out.get("case_id", "?"))
            return False
    return True


def safe_payload(alert: dict, ncfg: NotifyConfig, *,
                 scan_id: str, source: str) -> dict | None:
    """Build the outbound record, or None if the guard tripped (drop, not send).

    Returns only allow-listed fields plus derived, non-sensitive ones.
    """
    out: dict[str, Any] = {k: alert.get(k) for k in ALLOWED_KEYS
                           if alert.get(k) is not None}

    raw_case_id = str(alert.get("case_id", ""))
    if not raw_case_id:
        log.error("notify: alert without case_id -- dropping")
        return None

    case_id = (_opaque(raw_case_id, ncfg.redact_salt) if ncfg.opaque_ids
               else raw_case_id)
    out["case_id"] = case_id

    # `note` is free text from CaseStore._why(); bounded in practice, but not by
    # contract. Opt-in only.
    if ncfg.include_note and alert.get("note"):
        out["note"] = str(alert["note"])

    scan_dt = _parse_scan(alert.get("scan") or scan_id)
    label, due = sla_for(str(alert.get("severity", "")), scan_dt, ncfg)
    out["sla_label"] = label
    out["sla_due_utc"] = due.strftime("%Y-%m-%dT%H:%MZ") if due else None
    out["scan"] = scan_dt.strftime("%Y-%m-%dT%H:%MZ")
    out["source"] = source
    out["link"] = (f"{ncfg.dashboard_url}/{ncfg.lang}/case/{quote(case_id)}"
                   if ncfg.dashboard_url else None)

    if not _assert_no_leak(out, alert):
        return None
    return out
