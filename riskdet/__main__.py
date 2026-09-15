"""python -m riskdet <command> -- the operational entry points.

Commands:
  check        offline readiness report; zero API calls, zero cost
  bootstrap    ONE-TIME archive build (~$0.67). Refuses to re-run.
  increment    idempotent MERGE of new/late rounds into the archive
  calibrate    fit on May -> freeze -> validate on June-July (paid extractions)
  run          detection scan at a REQUIRED --as-of event-time boundary
  scan-integrity  fast INTEGRITY-only scan of the newest rounds (per-minute loop)
  pull-logs    Cloud Logging evidence for one case (free reads, redacted)

Set RISKDET_DRY_RUN_ONLY=1 to preview every query's cost without executing.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import sys


def _asof(value: str) -> _dt.datetime:
    try:
        return _dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an ISO datetime (UTC event time, e.g. "
            f"2026-08-17T16:00:00)") from exc


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(name)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="riskdet", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="offline readiness, zero cost")
    sub.add_parser("bootstrap", help="one-time archive build (paid, ~$0.67)")
    sub.add_parser("increment", help="MERGE new/late rounds into the archive")
    sub.add_parser("calibrate", help="fit May -> freeze -> validate June-July")

    rp = sub.add_parser("run", help="detection scan at an event-time boundary")
    rp.add_argument("--as-of", type=_asof, required=True,
                    help="UTC event-time boundary (the leakage gate; REQUIRED)")
    rp.add_argument("--persistence-mid", type=_asof, default=None,
                    help="optional mid-window boundary for split-half persistence")

    ip = sub.add_parser("scan-integrity",
                        help="fast INTEGRITY-only scan of the newest rounds "
                             "(the per-minute loop); ABSOLUTE rules only")
    ip.add_argument("--as-of", type=_asof, default=None,
                    help="minute-resolution scan id (default: now UTC)")
    ip.add_argument("--since-serial", type=int, default=None,
                    help="override the serial watermark (default: own state, "
                         "else the archive head)")

    mp = sub.add_parser("monitor-logs",
                        help="per-minute Cloud Logging monitor: double-settle, "
                             "round replay, API path bypass (free; no BigQuery)")
    mp.add_argument("--as-of", type=_asof, default=None,
                    help="minute-resolution scan id (default: now UTC)")
    mp.add_argument("--window-min", type=int, default=1,
                    help="window size on a cold start, in minutes (default 1)")
    mp.add_argument("--since", type=_asof, default=None,
                    help="override the timestamp watermark (UTC)")

    lp = sub.add_parser("pull-logs", help="Cloud Logging evidence for one case")
    lp.add_argument("--case-id", required=True)
    lp.add_argument("--evidence", required=True,
                    help="pipe-separated gsid@timestamp keys")
    lp.add_argument("--pad-min", type=int, default=1)

    l4 = sub.add_parser("layer4",
                        help="LLM agent chain (Vertex): gate -> investigate -> "
                             "skeptic -> compose. Reads the last scan's "
                             "candidates.jsonl; no BigQuery scan.")
    l4.add_argument("--as-of", type=_asof, required=True,
                    help="scan boundary that produced candidates.jsonl (the "
                         "scan_id for the case-state gate)")
    l4.add_argument("--max-cases", type=int, default=0,
                    help="cap cases investigated this run (0 = all queued)")
    l4.add_argument("--dry-run", action="store_true",
                    help="gate only and list the L4 queue; zero LLM calls/cost")

    args = p.parse_args(argv)

    import riskdet  # noqa: PLC0415
    cfg = riskdet.settings()

    if args.cmd == "check":
        rep = riskdet.check(cfg)
        print(json.dumps(rep, indent=2, default=str))
        return 0 if rep["ok"] else 1

    if args.cmd == "monitor-logs":
        # Reads Cloud Logging only -- free, and deliberately dispatched BEFORE
        # CostGuardedBQ is constructed so the per-minute loop never touches
        # BigQuery.
        from .cloudlogs import scan_logs  # noqa: PLC0415
        summary = scan_logs(cfg, as_of=args.as_of,
                            window_minutes=args.window_min, since=args.since)
        print(json.dumps(summary, indent=2, default=str))
        return 0

    if args.cmd == "pull-logs":
        from .cloudlogs import LogReader  # noqa: PLC0415
        from .signals import EvidenceRef  # noqa: PLC0415
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        evs = [EvidenceRef.parse(k, now_utc=now,
                                 retention_days=cfg.evidence_days)
               for k in args.evidence.split("|") if k]
        results = LogReader(cfg).pull_case(args.case_id, evs,
                                           pad_min=args.pad_min)
        pulled = sum(1 for r in results if r.file)
        print(f"pulled {pulled}/{len(results)} rounds -> {cfg.paths.logs_dir}")
        return 0

    if args.cmd == "layer4":
        from .layer4 import run as run_layer4  # noqa: PLC0415
        summary = run_layer4(cfg, args.as_of, max_cases=args.max_cases,
                             dry_run=args.dry_run)
        print(json.dumps(summary, indent=2, default=str))
        errored = any(
            (c.get("investigator", {}).get("is_error")
             or c.get("skeptic", {}).get("is_error"))
            for c in summary.get("cases", []) if isinstance(c, dict))
        return 1 if errored else 0

    from .bq import CostGuardedBQ  # noqa: PLC0415
    from .pipeline import bootstrap, ensure_replay_gate, increment  # noqa: PLC0415
    bq = CostGuardedBQ(cfg)

    if args.cmd == "bootstrap":
        bootstrap(bq)
        ensure_replay_gate(bq)
        return 0
    if args.cmd == "increment":
        increment(bq)
        return 0
    if args.cmd == "calibrate":
        from .pipeline.run import calibrate_cycle  # noqa: PLC0415
        verdict = calibrate_cycle(bq)
        print(f"calibration verdict: {verdict}")
        return 0 if verdict != "FAIL" else 1
    if args.cmd == "run":
        from .pipeline.run import scan  # noqa: PLC0415
        summary = scan(bq, args.as_of, persistence_mid=args.persistence_mid)
        print(json.dumps(summary, indent=2, default=str))
        return 0
    if args.cmd == "scan-integrity":
        from .pipeline.fast_integrity import scan_integrity  # noqa: PLC0415
        summary = scan_integrity(bq, as_of=args.as_of,
                                 since_serial=args.since_serial)
        print(json.dumps(summary, indent=2, default=str))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
