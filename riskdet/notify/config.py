"""Resolution of alert-delivery settings: YAML policy + environment secrets.

Deliberately NOT folded into `riskdet.config.Settings`. That dataclass is frozen,
constructed in one place and imported everywhere; adding delivery fields to it
would widen the blast radius of a feature that must never be able to break a
scan. `notify_settings(cfg)` reads the already-loaded `cfg.thresholds` plus the
environment, so nothing else changes.

Split of responsibilities, following the project rule that secrets come from the
environment ONLY:
  config/thresholds.yaml `notify:`  routing policy, SLA labels, caps
  environment                       bot token (Secret Manager), chat id, URLs

Fail-safe by construction: `dry_run` is forced ON whenever the token or chat id
is missing, so running a scan on a laptop can never page the risk team.
"""
from __future__ import annotations

import datetime as _dt
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings, _env_bool, _env_int

# SLA policy. These literal hours are contractually meaningful (they mirror the
# human-facing SLA in .claude/agents/report-composer.md), which is exactly the
# exemption the "no guessed constants" rule grants. YAML overrides them.
_SLA_DEFAULT: dict[str, dict[str, Any]] = {
    "Critical": {"label": "1h", "hours": 1},
    "High": {"label": "same-day", "hours": "end_of_day"},
    "Medium": {"label": "48h", "hours": 48},
    "Low": {"label": "weekly", "hours": 168},
}

_TELEGRAM_SEVERITIES = ("Critical", "High")


@dataclass(frozen=True)
class NotifyConfig:
    """Everything `deliver()` needs. Never contains a resolved secret in repr."""

    enabled: bool
    dry_run: bool
    source: str                       # "daily" | "integrity"

    telegram_token: str | None
    telegram_chat_id: str | None

    dashboard_url: str
    lang: str

    telegram_severities: tuple[str, ...]
    sla: dict[str, dict[str, Any]]

    max_messages_per_run: int
    per_minute_cap: int
    run_budget_seconds: float
    dedupe_window_minutes: int

    include_note: bool
    opaque_ids: bool
    redact_salt: str | None

    out_dir: Path

    # -- derived artifact paths (all under out/, i.e. the shared bucket) ---- #
    @property
    def state_path(self) -> Path:
        """Rate-limit bucket + last_gate_scan marker.

        Per-SOURCE file on purpose: the daily job and the per-minute integrity
        job share one gcsfuse mount, which has no locking. Separate objects mean
        the two writers can never corrupt each other.
        """
        return self.out_dir / f"notify_state_{self.source}.json"

    @property
    def spool_path(self) -> Path:
        """Critical/High that missed the run budget, retried by the next run."""
        return self.out_dir / "notify_spool.jsonl"

    @property
    def sent_path(self) -> Path:
        """Cross-cadence de-dupe: the daily and integrity stores can both alert
        on the same case_id minutes apart."""
        return self.out_dir / "notify_sent.json"

    @property
    def outbox_path(self) -> Path:
        """Dry-run sink -- what WOULD have been sent, for inspection."""
        return self.out_dir / "notify_outbox.jsonl"

    def __repr__(self) -> str:  # never leak the token into a log or traceback
        tok = "set" if self.telegram_token else "unset"
        return (f"NotifyConfig(enabled={self.enabled}, dry_run={self.dry_run}, "
                f"source={self.source!r}, token={tok}, "
                f"chat_id={'set' if self.telegram_chat_id else 'unset'}, "
                f"severities={self.telegram_severities})")


def notify_settings(cfg: Settings, *, source: str = "daily") -> NotifyConfig:
    """Resolve delivery configuration. Pure: reads cfg.thresholds + os.environ."""
    n: dict[str, Any] = dict(cfg.thresholds.get("notify", {}) or {})
    routing: dict[str, Any] = dict(n.get("routing", {}) or {})
    tg: dict[str, Any] = dict(n.get("telegram", {}) or {})

    token = os.environ.get("RISKDET_TELEGRAM_BOT_TOKEN") or None
    chat_id = os.environ.get("RISKDET_TELEGRAM_CHAT_ID") or None

    # Fail safe: no credentials -> dry run, whatever the flags say.
    dry_run = _env_bool("RISKDET_NOTIFY_DRY_RUN", False) or not (token and chat_id)

    sla = dict(_SLA_DEFAULT)
    for sev, spec in (n.get("sla", {}) or {}).items():
        if isinstance(spec, dict):
            sla[sev] = {**sla.get(sev, {}), **spec}

    sevs = routing.get("telegram_severities") or list(_TELEGRAM_SEVERITIES)

    return NotifyConfig(
        enabled=bool(n.get("enabled", True)) and _env_bool("RISKDET_NOTIFY_ENABLED", True),
        dry_run=dry_run,
        source=source,
        telegram_token=token,
        telegram_chat_id=chat_id,
        dashboard_url=(os.environ.get("RISKDET_DASHBOARD_URL") or "").rstrip("/"),
        lang=os.environ.get("RISKDET_NOTIFY_LANG") or "zh",
        telegram_severities=tuple(str(s) for s in sevs),
        sla=sla,
        max_messages_per_run=int(tg.get("max_messages_per_run", 8)),
        per_minute_cap=int(tg.get("per_minute_cap", 18)),
        run_budget_seconds=float(tg.get("run_budget_seconds", 20)),
        dedupe_window_minutes=_env_int(
            "RISKDET_NOTIFY_DEDUPE_MINUTES", int(n.get("dedupe_window_minutes", 60))),
        include_note=_env_bool("RISKDET_NOTIFY_INCLUDE_NOTE", False),
        opaque_ids=_env_bool("RISKDET_NOTIFY_OPAQUE_IDS", False),
        redact_salt=cfg.redact_salt,
        out_dir=cfg.paths.out,
    )


def sla_for(severity: str, scan_utc: _dt.datetime, ncfg: NotifyConfig
            ) -> tuple[str, _dt.datetime | None]:
    """-> (human label, due-by UTC). Unknown severity degrades to (severity, None)
    rather than raising -- a delivery path must never fail on a new enum value."""
    spec = ncfg.sla.get(severity)
    if not spec:
        return severity, None
    hours = spec.get("hours")
    label = str(spec.get("label", severity))
    if hours == "end_of_day":
        due = scan_utc.replace(hour=23, minute=59, second=0, microsecond=0)
    elif isinstance(hours, (int, float)):
        due = scan_utc + _dt.timedelta(hours=float(hours))
    else:
        due = None
    return label, due
