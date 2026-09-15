"""Per-minute Cloud Logging MONITOR -- the fast tier of real-time detection.

This is the counterpart to `pipeline/fast_integrity.py` (the 5-minute BigQuery
sweep). It exists because a set of risk events is **structurally invisible to
BigQuery** and can only ever be seen in the spin-server log stream:

  LOG-DOUBLE_SETTLE  a gameSeqId answered by MORE THAN ONE response. BigQuery's
                     L1-DUP_ROUND can only say "duplicated"; the spin-server
                     response count is the ONLY fact that separates a real
                     double-settle from an ETL duplicate.
  LOG-ROUND_REPLAY   one gameSeqId that FAILED and later SUCCEEDED. A failed
                     spin never reaches RecordSlot at all, so a client that
                     retries a round until it pays leaves zero trace in
                     BigQuery. This is the "retry-until-favourable" blind spot
                     named in the README.
  LOG-PATH_BYPASS    a request that did not arrive on the normal spin path, or
                     an unregistered agent calling the API. Neither `path` nor
                     `agent` exists in BigQuery.
  (cadence)          log timestamps are microsecond-resolution while BigQuery
                     `game_time` is 1-second, so millisecond cadence is
                     measurable here and nowhere else. See the note below on
                     why v1 OBSERVES rather than fires.

Rule class governs what may fire, exactly as in Layer 1 (CLAUDE.md):
  * DOUBLE_SETTLE / ROUND_REPLAY / PATH_BYPASS are ABSOLUTE -- deterministic,
    true on a single occurrence, no baseline, no volume floor. They fire.
  * CADENCE is STATISTICAL. The retired BOT_CADENCE rule failed precisely
    because it guessed a fixed <700ms constant. v1 therefore ACCUMULATES a gap
    census for later calibration and never fires on a per-minute sample.

Cost: Cloud Logging reads are free, so a per-minute cadence costs nothing. One
minute of spin-server traffic is ~7k entries / ~15s to fetch and parse.

Security: these payloads carry a LIVE JWT in `params.token` (observed in real
802 errors). Everything is passed through the shared Redactor on read and the
raw token is never persisted, logged, or emitted.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from collections import Counter, defaultdict
from typing import Any

from ..casestore import CaseStore
from ..config import GAME_CONTAINERS, Settings, credentials_for
from ..merge import fuse
from ..signals import EvidenceRef, Signal
from .redact import Redactor

log = logging.getLogger("riskdet.logmonitor")

# Cloud Logging ingestion is not instantaneous; never read right up to `now` or
# late-arriving entries would be missed and the watermark would skip them.
INGEST_LAG_GUARD_S = 90
# Re-read this far behind the watermark so a round whose request and response
# straddle a window boundary is still paired.
OVERLAP_S = 60
DEFAULT_WINDOW_MIN = 1
# Safety cap; ~7k entries/min is normal, so this is ~7x headroom before we would
# rather truncate than run unboundedly long inside a per-minute loop.
MAX_ENTRIES = 50_000

# The only shape a legitimate spin request takes, observed across live traffic:
#   POST /v2/<game_id>/spin
_SPIN_PATH = re.compile(r"^/v2/[A-Za-z0-9_]+/spin$")
_ALLOWED_METHODS = frozenset({"POST"})

# The settlement message. Counting anything else per gameSeqId is what makes
# ordinary rounds look double-settled -- see _fetch().
_RESPONSE_LABEL = "Spin-Server Response info"

_SUCCESS = 1000          # messageCode/resCode for a settled spin
_REQUEST_LOG = 0         # inbound request log line (carries path/method)
_JSON = re.compile(r"\{.*\}", re.S)


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def _parse(entry) -> dict | None:
    """One log entry -> a flat record, or None if it is not spin traffic."""
    payload = entry.payload if isinstance(entry.payload, str) else str(entry.payload)
    m = _JSON.search(payload)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None

    params = d.get("params") or {}
    resp = d.get("response") or {}
    data = d.get("data") or {}
    if not isinstance(params, dict):
        params = {}
    if not isinstance(resp, dict):
        resp = {}
    if not isinstance(data, dict):
        data = {}

    # gameSeqId lives in the response on a settled spin, and in params on a
    # FAILED attempt (observed on real 802s) -- which is what makes replay
    # detection possible at all.
    gsid = resp.get("gameSeqId") or params.get("gameSeqId")
    code = d.get("resCode")
    if code is None:
        code = data.get("messageCode")

    return {
        "ts": entry.timestamp,
        "gsid": gsid,
        "res_code": code,
        "path": d.get("path"),
        "method": d.get("method"),
        "agent": params.get("agent"),
        "game_id": params.get("gameId") or params.get("gameID"),
        "sm_tag": params.get("sm_tag"),
        "currency": params.get("currency"),
        # correlation-preserving digest of the JWT; the raw token never escapes
        # this function. Comparable WITHIN one run (salt is per-run unless
        # RISKDET_REDACT_SALT is pinned), which is all a 1-minute window needs.
        "player_key": None,
        "_token": params.get("token"),
        "message": str(data.get("message") or "")[:200],
        "pod": (getattr(entry, "resource", None).labels.get("pod_name", "")
                if getattr(entry, "resource", None) else ""),
    }


def _base_filter(start: _dt.datetime, end: _dt.datetime) -> str:
    return (f'resource.type="k8s_container" AND '
            f'resource.labels.container_name="spin-server" AND '
            f'timestamp>="{start:%Y-%m-%dT%H:%M:%S}Z" AND '
            f'timestamp<="{end:%Y-%m-%dT%H:%M:%S}Z"')


def _fetch(cfg: Settings, start: _dt.datetime, end: _dt.datetime,
           redactor: Redactor) -> tuple[list[dict], list[dict]]:
    """Pull one window of spin traffic. Free (Cloud Logging list).

    TWO narrow, server-side-filtered queries rather than one broad sweep --
    unfiltered spin-server traffic is ~18k entries/min and cannot be fetched
    inside a per-minute budget.

    A) RESPONSE messages only (`Info Spin-Server Response info -`). This is the
       settlement record, and it is the ONLY message type that may be counted
       for double-settle. The internal `[Core-Play] .../betNsettle` call also
       carries the same gameSeqId with resCode 1000, so counting both makes
       ~9% of ordinary rounds look double-settled. Error responses (802/901)
       carry this same label, so this one query also feeds replay detection and
       the resCode census.
    B) Requests whose path is NOT the normal spin path -- a negative filter, so
       it returns nothing at all on healthy traffic.
    """
    from google.cloud import logging_v2  # noqa: PLC0415

    creds, identity = credentials_for("logging", cfg)
    client = logging_v2.Client(project=cfg.log_project, credentials=creds)
    base = _base_filter(start, end)

    def _collect(flt: str) -> list[dict]:
        rows: list[dict] = []
        for entry in client.list_entries(filter_=flt, page_size=1000,
                                         max_results=MAX_ENTRIES):
            rec = _parse(entry)
            if rec is None:
                continue
            tok = rec.pop("_token", None)
            if tok:
                # digest immediately; the raw credential is dropped here and
                # never reaches a file, a log line, or a Signal.
                rec["player_key"] = redactor.redact(str(tok)).payload
            rows.append(rec)
        return rows

    responses = _collect(f'{base} AND textPayload:"{_RESPONSE_LABEL}"')
    off_path = _collect(f'{base} AND textPayload:"\\"path\\":" '
                        f'AND NOT textPayload:"/spin"')

    log.info("log monitor: %d response(s), %d off-path request(s) as %s",
             len(responses), len(off_path), identity)
    return responses, off_path


# --------------------------------------------------------------------------- #
# detectors -- ABSOLUTE only; statistical signals accumulate instead
# --------------------------------------------------------------------------- #
def _detect_double_settle(recs: list[dict]) -> list[dict]:
    """>1 SETTLED response for one gameSeqId. Deterministic double-settle: the
    one question BigQuery can never answer for an L1-DUP_ROUND."""
    by: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        if r["gsid"] and r["res_code"] == _SUCCESS:
            by[r["gsid"]].append(r)
    return [{"gsid": g, "responses": len(v), "rows": v}
            for g, v in by.items() if len(v) > 1]


def _detect_round_replay(recs: list[dict]) -> list[dict]:
    """One gameSeqId that FAILED and also SUCCEEDED -> the round was replayed.

    A lone failure is NOT a finding: 802 is "Cash Balance not enough", which is
    ordinary. Only the failure->success pair on the SAME round id is
    deterministic evidence that an attempt was repeated until it paid.
    """
    codes: dict[str, set] = defaultdict(set)
    rows: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        if not r["gsid"] or r["res_code"] is None or r["res_code"] == _REQUEST_LOG:
            continue
        codes[r["gsid"]].add(r["res_code"])
        rows[r["gsid"]].append(r)
    out = []
    for g, cs in codes.items():
        failed = {c for c in cs if c != _SUCCESS}
        if failed and _SUCCESS in cs:
            out.append({"gsid": g, "codes": sorted(cs),
                        "failed_codes": sorted(failed), "rows": rows[g]})
    return out


def _detect_path_bypass(recs: list[dict]) -> list[dict]:
    """Requests that did not arrive on the normal spin path, or that name an
    agent the platform does not recognise ("Agent not found" / 901).

    Both are requests that should not be reaching the game API at all, and
    neither `path` nor `agent` exists anywhere in BigQuery.
    """
    out = []
    for r in recs:
        if r["path"] and not _SPIN_PATH.match(str(r["path"])):
            out.append({"kind": "path", "detail": str(r["path"]), "row": r})
        elif r["method"] and str(r["method"]) not in _ALLOWED_METHODS:
            out.append({"kind": "method", "detail": str(r["method"]), "row": r})
        elif r["agent"] and "agent not found" in r["message"].lower():
            out.append({"kind": "unknown_agent",
                        "detail": str(r["agent"]), "row": r})
    return out


def _cadence_census(recs: list[dict]) -> dict:
    """Millisecond gap census per player key -- OBSERVATION ONLY in v1.

    Deliberately does not fire. `game_time` in BigQuery is 1-second, so these
    sub-second gaps are genuinely new information, but turning them into a rule
    requires a calibrated population baseline. Guessing a fixed millisecond
    threshold is exactly the mistake that got BOT_CADENCE retired. This census
    is the raw material for that calibration.
    """
    by: dict[str, list] = defaultdict(list)
    for r in recs:
        if r["player_key"] and r["res_code"] == _SUCCESS:
            by[r["player_key"]].append(r["ts"])
    gaps: list[float] = []
    per_player: dict[str, dict] = {}
    for key, times in by.items():
        if len(times) < 2:
            continue
        ts = sorted(times)
        g = [(ts[i + 1] - ts[i]).total_seconds() for i in range(len(ts) - 1)]
        gaps.extend(g)
        per_player[key] = {"rounds": len(ts), "min_gap_s": min(g),
                           "median_gap_s": sorted(g)[len(g) // 2]}
    buckets = Counter()
    for g in gaps:
        if g < 0.05:
            buckets["<50ms"] += 1
        elif g < 0.2:
            buckets["50-200ms"] += 1
        elif g < 1.0:
            buckets["200ms-1s"] += 1
        else:
            buckets[">=1s"] += 1
    return {"players_with_2plus_rounds": len(per_player),
            "gaps_measured": len(gaps),
            "gap_buckets": dict(buckets),
            "fastest_players": sorted(
                per_player.items(), key=lambda kv: kv[1]["min_gap_s"])[:5]}


# --------------------------------------------------------------------------- #
# player attribution (only for HITS -- cheap because hits are rare)
# --------------------------------------------------------------------------- #
def _attribute(cfg: Settings, gsids: list[str], start: _dt.datetime,
               end: _dt.datetime) -> dict[str, dict]:
    """Resolve gameSeqId -> (parent, uid) from play-go, which carries them in
    plaintext (spin-server keeps uid inside the JWT, so it is unusable there).

    Runs ONLY for rounds that already tripped a detector, so the expensive
    high-volume container is queried a handful of times, not every minute.
    """
    if not gsids:
        return {}
    from google.cloud import logging_v2  # noqa: PLC0415

    creds, _ = credentials_for("logging", cfg)
    client = logging_v2.Client(project=cfg.log_project, credentials=creds)
    found: dict[str, dict] = {}
    for gsid in gsids[:25]:                      # bound the work
        flt = (f'resource.type="k8s_container" AND '
               f'resource.labels.container_name="play-go" AND '
               f'timestamp>="{start:%Y-%m-%dT%H:%M:%S}Z" AND '
               f'timestamp<="{end:%Y-%m-%dT%H:%M:%S}Z" AND '
               f'textPayload:"{gsid}"')
        try:
            for entry in client.list_entries(filter_=flt, page_size=10,
                                             max_results=10):
                rec = _parse(entry)
                payload = (entry.payload if isinstance(entry.payload, str)
                           else str(entry.payload))
                m = _JSON.search(payload)
                if not m:
                    continue
                blob = json.loads(m.group(0))
                ctx = json.dumps(blob)
                pu = re.search(r'"parent"\s*:\s*"([^"]+)"', ctx)
                uu = re.search(r'"uid"\s*:\s*"([^"]+)"', ctx)
                if pu and uu:
                    found[gsid] = {"parent": pu.group(1), "uid": uu.group(1),
                                   "game_id": (rec or {}).get("game_id")}
                    break
        except Exception as exc:                  # noqa: BLE001
            log.warning("attribution failed for %s: %s", gsid, type(exc).__name__)
    return found


# --------------------------------------------------------------------------- #
# signals
# --------------------------------------------------------------------------- #
def _ev(gsid: str, ts) -> tuple[EvidenceRef, ...]:
    try:
        key = f"{gsid}@{ts:%Y-%m-%dT%H:%M:%SZ}"
        return (EvidenceRef.parse(
            key, now_utc=_dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None),
            retention_days=30),)
    except Exception:                             # noqa: BLE001
        return ()


def _signals(dbl: list[dict], replay: list[dict], bypass: list[dict],
             who: dict[str, dict]) -> list[Signal]:
    out: list[Signal] = []

    for d in dbl:
        w = who.get(d["gsid"], {})
        r0 = d["rows"][0]
        out.append(Signal(
            signal_id="LOG-DOUBLE_SETTLE", family="INTEGRITY", layer=1,
            grain="player_cell" if w else "game_cell",
            parent=w.get("parent"), uid=w.get("uid"),
            game_id=w.get("game_id") or r0.get("game_id"), play_type=None,
            currency=r0.get("currency"), sm_tag=r0.get("sm_tag"),
            metric="spin_responses_per_round", value=float(d["responses"]),
            threshold=1.0, threshold_method="ABSOLUTE",
            baseline_source="identity", n=d["responses"],
            persistence="not_computable",
            description=(f"gameSeqId {d['gsid']} was answered by "
                         f"{d['responses']} spin-server responses -- the log "
                         f"response count is the only fact that distinguishes "
                         f"a real double-settle from an ETL duplicate"),
            context={"game_seq_id": d["gsid"], "requires_log_adjudication": False},
            evidence=_ev(d["gsid"], r0["ts"])))

    for d in replay:
        w = who.get(d["gsid"], {})
        r0 = d["rows"][0]
        out.append(Signal(
            signal_id="LOG-ROUND_REPLAY", family="INTEGRITY", layer=1,
            grain="player_cell" if w else "game_cell",
            parent=w.get("parent"), uid=w.get("uid"),
            game_id=w.get("game_id") or r0.get("game_id"), play_type=None,
            currency=r0.get("currency"), sm_tag=r0.get("sm_tag"),
            metric="round_replay_codes", value=float(len(d["codes"])),
            threshold=1.0, threshold_method="ABSOLUTE",
            baseline_source="identity", n=len(d["rows"]),
            persistence="not_computable",
            description=(f"gameSeqId {d['gsid']} FAILED "
                         f"(resCode {d['failed_codes']}) and later SUCCEEDED -- "
                         f"the round was retried after an unfavourable "
                         f"outcome; failed attempts never reach RecordSlot"),
            context={"game_seq_id": d["gsid"], "res_codes": d["codes"]},
            evidence=_ev(d["gsid"], r0["ts"])))

    # bypass is an attribute of the REQUEST, not of a player -> game/ops level
    by_kind: dict[tuple, list] = defaultdict(list)
    for b in bypass:
        by_kind[(b["kind"], b["detail"])].append(b["row"])
    for (kind, detail), rows in by_kind.items():
        out.append(Signal(
            signal_id="LOG-PATH_BYPASS", family="ENVIRONMENT", layer=1,
            grain="game_cell", parent=None, uid=None,
            game_id=rows[0].get("game_id"), play_type=None,
            currency=rows[0].get("currency"), sm_tag=rows[0].get("sm_tag"),
            metric=f"unexpected_{kind}", value=float(len(rows)),
            threshold=0.0, threshold_method="ABSOLUTE",
            baseline_source="identity", n=len(rows),
            persistence="not_computable",
            description=(f"{len(rows)} request(s) with unexpected {kind} "
                         f"{detail!r} -- legitimate spin traffic is "
                         f"POST /v2/<game_id>/spin from a registered agent"),
            context={"kind": kind, "detail": detail,
                     "event_date": f"{rows[0]['ts']:%Y-%m-%d}"},
            evidence=()))
    return out


# --------------------------------------------------------------------------- #
# watermark
# --------------------------------------------------------------------------- #
def _wm_path(cfg: Settings):
    return cfg.paths.out / "logmon_watermark.json"


def _read_wm(cfg: Settings) -> _dt.datetime | None:
    p = _wm_path(cfg)
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8")).get("through_utc")
        return _dt.datetime.fromisoformat(raw) if raw else None
    except (ValueError, OSError):
        return None


def _write_wm(cfg: Settings, through: _dt.datetime, scan_id: str) -> None:
    p = _wm_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"through_utc": through.isoformat(),
                             "updated": scan_id}, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
def scan_logs(cfg: Settings, *, as_of: _dt.datetime | None = None,
              window_minutes: int = DEFAULT_WINDOW_MIN,
              since: _dt.datetime | None = None) -> dict:
    """One per-minute log monitoring pass. Returns a summary dict."""
    now = as_of or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    scan_id = now.replace(second=0, microsecond=0).isoformat()

    end = now - _dt.timedelta(seconds=INGEST_LAG_GUARD_S)
    if since is not None:
        start = since
    else:
        wm = _read_wm(cfg)
        start = (wm - _dt.timedelta(seconds=OVERLAP_S)) if wm else (
            end - _dt.timedelta(minutes=window_minutes))
    if start >= end:
        return {"scan_id": scan_id, "executed": False,
                "note": "window not yet open (ingestion lag guard)"}

    redactor = Redactor(cfg.redact_salt)
    recs, off_path = _fetch(cfg, start, end, redactor)

    # Double-settle / replay / cadence read ONLY the settlement responses.
    dbl = _detect_double_settle(recs)
    replay = _detect_round_replay(recs)
    census = _cadence_census(recs)
    # Bypass reads the off-path requests plus any unregistered-agent errors,
    # which arrive on the response stream.
    bypass = _detect_path_bypass(off_path) + [
        b for b in _detect_path_bypass(recs) if b["kind"] == "unknown_agent"]

    who = _attribute(cfg, [d["gsid"] for d in dbl] + [d["gsid"] for d in replay],
                     start, end) if (dbl or replay) else {}

    signals = _signals(dbl, replay, bypass, who)
    candidates = fuse(cfg, signals) if signals else []

    store = CaseStore(cfg.paths.out / "case_state_logmon.json")
    cand_dicts = [{"case_id": c.case_id, "escalation": c.escalation,
                   "severity": c.severity, "families": sorted(c.families),
                   "parent": c.parent, "uid": c.uid, "game_id": c.game_id,
                   "currency": c.currency,
                   "signals": [s.signal_id for s in c.signals],
                   "descriptions": [s.description for s in c.signals]}
                  for c in candidates]
    alerts = store.ingest(scan_id, cand_dicts)
    store.save()

    cfg.paths.out.mkdir(parents=True, exist_ok=True)
    (cfg.paths.out / "candidates_logmon.jsonl").write_text(
        "".join(json.dumps(d, default=str) + "\n" for d in cand_dicts),
        encoding="utf-8")
    if alerts:
        with (cfg.paths.out / "alerts_logmon.jsonl").open("a", encoding="utf-8") as fh:
            for a in alerts:
                fh.write(json.dumps({"scan_id": scan_id, **a}, default=str) + "\n")
    (cfg.paths.out / "logmon_cadence_census.json").write_text(
        json.dumps({"scan_id": scan_id, **census}, indent=2, default=str),
        encoding="utf-8")

    _write_wm(cfg, end, scan_id)

    summary = {
        "scan_id": scan_id, "executed": True,
        "window": [start.isoformat(), end.isoformat()],
        "records": len(recs),
        "off_path_requests": len(off_path),
        "double_settle": len(dbl),
        "round_replay": len(replay),
        "path_bypass": len(bypass),
        "res_codes": dict(Counter(r["res_code"] for r in recs
                                  if r["res_code"] is not None)),
        "cadence_census": {k: census[k] for k in
                           ("players_with_2plus_rounds", "gaps_measured",
                            "gap_buckets")},
        "candidates": len(candidates),
        "new_alerts": len(alerts),
        "attributed": len(who),
    }
    log.info("log monitor %s: %d records, %d double-settle, %d replay, "
             "%d bypass, %d new alert(s)", scan_id, len(recs), len(dbl),
             len(replay), len(bypass), len(alerts))
    return summary
