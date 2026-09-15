"""Cloud Logging evidence puller -- turns a statistical candidate into (or out
of) a finding by fetching what actually happened, round by round.

Windowing: +/-pad_min (default 1) around EACH evidence round's event time --
never around the account (whose activity can span months). The tight window is
what keeps a substring scan over prod logs fast, and it is exactly what the
`game_seq_id@timestamp` evidence keys were designed for.

Join key: game_seq_id ONLY. Verified against live logs: uid appears in
spin-server payloads exclusively INSIDE a base64 JWT, so a uid substring
search silently misses every spin-server entry. game_seq_id is plaintext in
all five gameplay containers.

Identity: airc-740@acp-prod (the same key the whole package uses -- it holds
BigQuery on acp-develop and Logs Viewer on acp-prod). The ambient ADC cannot
read acp-prod logs, and config.check() flags that before anything runs.

Retention: the _Default bucket keeps 30 days. Rounds older than that are
SKIPPED and counted -- a candidate whose evidence has aged out is reported as
un-investigable, never silently empty.

What the logs add that BigQuery cannot: slotWindow (the actual reels),
beforeBalance/afterBalance, mathVer, requestId, resCode, the internal
betNsettle call, and the spin-server RESPONSE COUNT per round -- the only
fact that separates a double-settle from an ETL duplicate.
"""

from __future__ import annotations

import csv
import datetime as _dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..config import GAME_CONTAINERS, Settings, credentials_for
from ..paths import sanitize
from ..signals import EvidenceRef
from .redact import Redactor, dumps_redacted

log = logging.getLogger("riskdet.cloudlogs")


@dataclass
class RoundEvidence:
    game_seq_id: str
    round_time_utc: _dt.datetime
    entries: int = 0
    by_container: dict[str, int] = field(default_factory=dict)
    spin_server_responses: int = 0
    redactions: int = 0
    file: Path | None = None
    skipped_reason: str | None = None


class LogReader:
    def __init__(self, cfg: Settings) -> None:
        from google.cloud import logging_v2  # noqa: PLC0415
        creds, identity = credentials_for("logging", cfg)
        self.identity = identity
        self.cfg = cfg
        self._client = logging_v2.Client(project=cfg.log_project,
                                         credentials=creds)
        self._redactor = Redactor(cfg.redact_salt)

    # ------------------------------------------------------------------ #
    def _filter(self, gsid: str, start: _dt.datetime, end: _dt.datetime,
                containers: tuple[str, ...]) -> str:
        cont = " OR ".join(f'resource.labels.container_name="{c}"'
                           for c in containers)
        return (
            f'resource.type="k8s_container" AND ({cont}) AND '
            f'timestamp>="{start:%Y-%m-%dT%H:%M:%S}Z" AND '
            f'timestamp<="{end:%Y-%m-%dT%H:%M:%S}Z" AND '
            f'textPayload:"{gsid}"')

    def fetch_round(self, case_id: str, ev: EvidenceRef,
                    *, pad_min: int = 1, max_entries: int = 200,
                    containers: tuple[str, ...] = GAME_CONTAINERS
                    ) -> RoundEvidence:
        if ev.game_time_utc is None:
            return RoundEvidence(ev.game_seq_id, _dt.datetime.min,
                                 skipped_reason="evidence key has no timestamp")
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        if not ev.log_retention_ok or (now - ev.game_time_utc) > _dt.timedelta(
                days=self.cfg.evidence_days):
            return RoundEvidence(
                ev.game_seq_id, ev.game_time_utc,
                skipped_reason=(f"round is older than the "
                                f"{self.cfg.evidence_days}-day log retention "
                                f"-- permanently un-investigable in logs"))

        pad = _dt.timedelta(minutes=pad_min)
        flt = self._filter(ev.game_seq_id, ev.game_time_utc - pad,
                           ev.game_time_utc + pad, containers)
        out = RoundEvidence(ev.game_seq_id, ev.game_time_utc)
        raw_entries: list[dict] = []
        for entry in self._client.list_entries(filter_=flt,
                                               page_size=min(max_entries, 100),
                                               max_results=max_entries):
            container = (entry.resource.labels or {}).get("container_name", "?")
            out.by_container[container] = out.by_container.get(container, 0) + 1
            payload = getattr(entry, "payload", None)
            text = payload if isinstance(payload, str) else None
            if container == "spin-server" and text and "Response" in text:
                out.spin_server_responses += 1
            raw_entries.append({
                "timestamp": entry.timestamp.isoformat()
                             if entry.timestamp else None,
                "container": container,
                "pod": (entry.resource.labels or {}).get("pod_name"),
                "severity": str(entry.severity or ""),
                "payload": payload if isinstance(payload, (str, dict))
                           else str(payload),
            })
        out.entries = len(raw_entries)

        dest = self.cfg.paths.log_evidence(case_id, ev.game_seq_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "case_id": case_id,
            "game_seq_id": ev.game_seq_id,
            "round_time_utc": f"{ev.game_time_utc:%Y-%m-%dT%H:%M:%SZ}",
            "window_minutes": pad_min,
            "filter": flt,
            "identity": self.identity,
            "entries": raw_entries,
        }
        # the ONLY serialisation path: redact -> assert clean -> write
        text, res = dumps_redacted(doc, self._redactor, where=str(dest),
                                   indent=2)
        dest.write_text(text, encoding="utf-8")
        out.redactions = res.redactions
        out.file = dest
        return out

    # ------------------------------------------------------------------ #
    def pull_case(self, case_id: str, evidence: list[EvidenceRef],
                  *, pad_min: int = 1, max_rounds: int = 10
                  ) -> list[RoundEvidence]:
        case_id = sanitize(case_id)
        results = []
        for ev in evidence[:max_rounds]:
            r = self.fetch_round(case_id, ev, pad_min=pad_min)
            results.append(r)
            if r.skipped_reason:
                log.info("%s %s: SKIP %s", case_id, ev.game_seq_id[:8],
                         r.skipped_reason)
            else:
                log.info("%s %s: %d entries %s spin_responses=%d",
                         case_id, ev.game_seq_id[:8], r.entries,
                         r.by_container, r.spin_server_responses)
        self._index(case_id, results)
        return results

    def _index(self, case_id: str, results: list[RoundEvidence]) -> None:
        path = self.cfg.paths.log_index
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["case_id", "game_seq_id", "round_time_utc",
                            "entries", "spin_server_responses",
                            "containers", "redactions", "skipped_reason",
                            "file"])
            for r in results:
                w.writerow([
                    case_id, r.game_seq_id,
                    f"{r.round_time_utc:%Y-%m-%dT%H:%M:%SZ}"
                    if r.round_time_utc != _dt.datetime.min else "",
                    r.entries, r.spin_server_responses,
                    ";".join(f"{k}={v}" for k, v in
                             sorted(r.by_container.items())),
                    r.redactions, r.skipped_reason or "",
                    str(r.file) if r.file else ""])
