"""RiskdetProvider -- serves the dashboard from REAL riskdet artifacts.

Reads (no BigQuery, no mock):
  out/candidates.jsonl        fused candidates (the detection output)
  output/case_queue.md        window + counts header
  output/cases/*.md           Layer-4 investigation / skeptic text (if present)
  output/reports/*.md         the composed daily digest (if present)
  output/data_quality/*.md    caveats (grepped for a `Verdict:` line)

Maps the engine's Candidate/Signal shapes onto the DataProvider contract in
base.py, so the UI is unchanged. Missing pieces degrade gracefully (a candidate
with no case file shows status 'new' and no AI text -- never invented).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import re

from .base import RISK_TYPES, SEVERITIES, DataProvider  # noqa: F401

# Default root = the GCP repo (dev/simulation data). Override with
# RISKDET_DASHBOARD_ROOT to point at a CLEAN production mirror (real GCS data
# only), so production viewing is never merged with the w1 simulation in the repo.
ROOT = (pathlib.Path(os.environ["RISKDET_DASHBOARD_ROOT"])
        if os.environ.get("RISKDET_DASHBOARD_ROOT")
        else pathlib.Path(__file__).resolve().parent.parent.parent)   # GCP/
RETENTION_DAYS = 30

# Absolute-integrity breaches. When the SAME breach hits >=2 accounts in the same
# (operator, game_id, play_type, sm_tag) on one scan day, that distribution is the
# fingerprint of a GAME/PLATFORM-level defect (e.g. a wrong math sheet), not N
# independent players -- so the dashboard collapses them into one grouped case.
_BREACH_SIDS = {"L1-MAX_X_BREACH", "L1-BALANCE_IDENTITY", "L1-DUP_ROUND"}
_BREACH_LABEL = {"L1-MAX_X_BREACH": ("cap breach", "超上限"),
                 "L1-BALANCE_IDENTITY": ("wallet-identity breach", "錢包帳務不符"),
                 "L1-DUP_ROUND": ("duplicate settlement", "重複結算")}

# signal family / id  ->  the 9 business risk types
_FAMILY_TYPE = {
    "OUTCOME_MAGNITUDE": "Abnormal RTP",
    "MAGNITUDE_WATCH": "Abnormal RTP",
    "PRESCIENCE": "Advantage Play",
    "ROUTING": "Feature Buy Abuse",
    "OUTCOME_FREQUENCY": "Suspicious Betting Pattern",
    "TIMING": "Bot/Automation",
    "ENVIRONMENT": "Game RTP Shift",
}
_SIGNAL_TYPE = {
    "L1-DUP_ROUND": "Settlement Anomaly",
    "L1-MAX_X_BREACH": "Settlement Anomaly",
    "L1-BALANCE_IDENTITY": "Balance Reconciliation",
    "L1-CONCURRENT": "Bot/Automation",
    "L2-OFF_TARGET_RTP": "Game RTP Shift",
    "L2-DEPLOY_SHIFT": "Game RTP Shift",
    "L4-LOG_PATTERN": "API Anomaly",
}
# order to pick the dominant signal when several fired
_TYPE_PRIORITY = ["Settlement Anomaly", "Balance Reconciliation", "API Anomaly",
                  "Advantage Play", "Feature Buy Abuse", "Bot/Automation",
                  "Suspicious Betting Pattern", "Abnormal RTP", "Game RTP Shift"]


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


class RiskdetProvider(DataProvider):
    def __init__(self, root: pathlib.Path | None = None) -> None:
        self.root = root or ROOT
        self._events: dict[str, dict] = {}
        self._order: list[dict] = []
        self._window = ("", "")
        self._load()

    # ------------------------------------------------------------------ #
    def _sources(self) -> list[pathlib.Path]:
        """Which candidate file(s) to serve.

        Default = the real single-window run (out/candidates.jsonl), so a live
        detection run is NEVER shadowed by sim data. Opt into the week-1 sim
        replay only with RISKDET_DASHBOARD_SOURCE=w1 -- then each per-day
        candidates_<date>.jsonl is loaded as that business day's cumulative queue.
        """
        import os
        if os.environ.get("RISKDET_DASHBOARD_SOURCE", "real").lower() == "w1":
            w1 = sorted((self.root / "out" / "w1").glob("candidates_*.jsonl"))
            if w1:
                return w1
        return [self.root / "out" / "candidates.jsonl"]

    def _load(self) -> None:
        self._dispositions = self._load_dispositions()   # human decisions, if any
        for jl in self._sources():
            if not jl.is_file():
                continue
            cands = [json.loads(x) for x in jl.read_text(encoding="utf-8").splitlines() if x.strip()]
            for c in cands:
                ev = self._to_event(c)
                self._events[ev["id"]] = ev
                w = (c.get("window_start", ""), c.get("window_end", ""))
                if w[1] > self._window[1]:
                    self._window = w
        self._build_groups()   # collapse shared integrity breaches into platform cases
        self._order = sorted(
            [e for e in self._events.values() if not e.get("_grouped_into")],
            key=lambda e: (e["detected_at"], e["_sevrank"]))

    # ------------------------------------------------------------------ #
    # Platform-defect grouping: collapse >=2 accounts sharing one absolute
    # integrity breach on the same (operator, game, play_type, sm_tag) per day.
    _GROUP_STATUS_PRIORITY = ["confirmed", "queued", "skeptic_review",
                              "investigating", "new", "downgraded", "rejected",
                              "monitoring", "closed"]

    @staticmethod
    def _group_id(op, game, pt, tag, sid, day) -> str:
        raw = f"G_{op}_{game}_pt{pt}_{tag}_{sid}@{day}"
        return re.sub(r"[^A-Za-z0-9_.@-]", "-", raw)

    def _build_groups(self) -> None:
        self._groups: dict[str, list[str]] = {}
        # ---- level 1: game-cell groups (>=2 accounts, same op+game+build+breach)
        buckets: dict[tuple, list[dict]] = {}
        for e in self._events.values():
            if e.get("grain") != "player" or not e.get("_breaches"):
                continue
            top_sid = (e.get("_top") or {}).get("signal_id")
            sid = top_sid if top_sid in _BREACH_SIDS else e["_breaches"][0]
            e["_pbreach"] = sid
            key = (e.get("_scan_day") or e["detected_at"][:10], e["operator"],
                   e["game_id"], e.get("play_type"), e.get("sm_tag"), sid)
            buckets.setdefault(key, []).append(e)
        cell_groups: dict[str, dict] = {}
        for key, members in buckets.items():
            if len(members) < 2:
                continue
            gid = self._group_id(*key[1:], key[0])
            for m in members:
                m["_grouped_into"] = gid
            self._groups[gid] = [m["id"] for m in members]
            ge = self._make_group_event(gid, key, members)
            cell_groups[gid] = ge
            self._events[gid] = ge

        # ---- level 2: build-wide rollup -- SAME breach on SAME build (sm_tag)
        # spanning >=2 distinct cells (games/operators). A single build defect
        # (e.g. a wrong math sheet) surfaces wherever the build is deployed, so a
        # cross-cell/cross-operator breach is ONE platform defect, not many.
        bw: dict[tuple, dict] = {}

        def _slot(k):
            return bw.setdefault(k, {"groups": [], "singletons": [],
                                     "cells": set(), "operators": set(),
                                     "accounts": 0, "per_op": {}, "peak": None,
                                     "cap": None})

        for gid, ge in cell_groups.items():
            g = ge["_group"]
            k = (ge.get("_scan_day") or ge["detected_at"][:10], ge["sm_tag"], g["sid"])
            s = _slot(k)
            s["groups"].append(gid)
            s["cells"].add((ge["operator"], ge["game_id"], ge["play_type"]))
            s["operators"].add(ge["operator"])
            s["accounts"] += g["n"]
            s["per_op"][ge["operator"]] = s["per_op"].get(ge["operator"], 0) + g["n"]
            s["peak"] = max([v for v in (s["peak"], g.get("peak")) if v is not None], default=None)
            s["cap"] = s["cap"] if s["cap"] is not None else g.get("cap")
        for e in self._events.values():
            if e.get("grain") != "player" or not e.get("_breaches") or e.get("_grouped_into"):
                continue
            k = (e.get("_scan_day") or e["detected_at"][:10], e.get("sm_tag"), e.get("_pbreach"))
            s = _slot(k)
            s["singletons"].append(e["id"])
            s["cells"].add((e["operator"], e["game_id"], e.get("play_type")))
            s["operators"].add(e["operator"])
            s["accounts"] += 1
            s["per_op"][e["operator"]] = s["per_op"].get(e["operator"], 0) + 1
            tv = (e.get("_top") or {}).get("value")
            if isinstance(tv, (int, float)):
                s["peak"] = max([v for v in (s["peak"], tv) if v is not None], default=None)
            if s["cap"] is None:
                s["cap"] = (e.get("_top") or {}).get("threshold")

        for k, s in bw.items():
            if len(s["cells"]) < 2:
                continue                      # confined to one cell -> level-1 only
            day, tag, sid = k
            bwid = re.sub(r"[^A-Za-z0-9_.@-]", "-", f"B_{tag}_{sid}@{day}")
            members = s["groups"] + s["singletons"]
            for mid in members:
                self._events[mid]["_grouped_into"] = bwid   # re-parent under build-wide
            self._groups[bwid] = members
            self._events[bwid] = self._make_buildwide_event(bwid, k, s, members)

    @staticmethod
    def _member_rounds(members, *, per_account: int = 2, cap: int = 24) -> list[dict]:
        """Aggregate each member account's top round evidence into one list for a
        grouped/platform case -- each round tagged with its account -- so the
        round evidence of every ID appears in the group report without the human
        having to drill into each account. `members` are per-account event dicts.
        An account with no round key on file still gets a placeholder line."""
        out: list[dict] = []
        for m in members:
            uid = m.get("uid") or "?"
            pv = (m.get("_top") or {}).get("value")
            peak = f" · peak {pv}" if isinstance(pv, (int, float)) else ""
            rounds = m.get("evidence_rounds") or []
            if rounds:
                for r in rounds[:per_account]:
                    out.append({"gsid": r.get("gsid", "?"), "time": r.get("time", "?"),
                                "note": f"account {uid}{peak}",
                                "log_status": r.get("log_status", "n/a")})
            else:
                out.append({"gsid": "—", "time": "—",
                            "note": f"account {uid}{peak} — no round key on file",
                            "log_status": "n/a"})
            if len(out) >= cap:
                break
        return out[:cap]

    def _make_group_event(self, gid, key, members) -> dict:
        day, op, game, pt, tag, sid = key
        members = sorted(members, key=lambda m: m["_sevrank"])
        n = len(members)
        worst = members[0]
        cap = (worst.get("_top") or {}).get("threshold")
        vals = [(m.get("_top") or {}).get("value") for m in members]
        vals_f = [v for v in vals if isinstance(v, (int, float))]
        peak = max(vals_f) if vals_f else None
        # aggregate status = most-actionable among members
        statuses = {m["status"] for m in members}
        status = next((s for s in self._GROUP_STATUS_PRIORITY if s in statuses),
                      worst["status"])
        label_en = _BREACH_LABEL.get(sid, ("integrity breach", "完整性破口"))[0]
        chart = {"kind": "bars",
                 "title": "Per-account breach vs certified reference",
                 "note": "each bar is one account's peak on this game; all breach the reference",
                 "labels": [m["uid"] for m in members], "series": vals,
                 "ref": cap, "sqrt": (sid == "L1-MAX_X_BREACH"),
                 "ylabel": ("win / bet (log)" if sid == "L1-MAX_X_BREACH" else "violations"),
                 "suspect": [True] * n,
                 "suspect_range": {"n": n, "start": members[0]["uid"], "end": members[-1]["uid"]}}
        why = [{"signal": sid, "family": "INTEGRITY",
                "value": peak, "threshold": cap,
                "method": "shared across accounts",
                "description": (f"{n} accounts under {op} on {game} share the same "
                                f"absolute breach ({sid}) — a distributed breach on one "
                                f"game points to a platform/game-level defect, not N "
                                f"independent players.")}]
        # Auto-summarise each member account's round evidence into the group
        # report, so a human sees the per-account rounds without drilling in.
        group_rounds = self._member_rounds(members)
        return {
            "id": gid, "case_id": gid,
            "detected_at": max(m["detected_at"] for m in members),   # date by latest activity
            "_scan_day": day,                                        # scan boundary (for build-wide grouping)
            "risk_type": "Game/Platform Defect", "severity": worst["severity"],
            "status": status, "_engine_status": status,
            "title": f"{game} {label_en} — {n} accounts @ {op}",
            "operator": op, "uid": "", "game_id": game,
            "currency": worst.get("currency", ""),
            "headline_metric": "", "_top": None, "_sevrank": worst["_sevrank"],
            "grain": "game_cell", "play_type": pt, "sm_tag": tag, "_breaches": [sid],
            "_group": {"level": "cell", "n": n, "sid": sid, "peak": peak, "cap": cap,
                       "members": [m["id"] for m in members],
                       "uids": [m["uid"] for m in members]},
            "window": worst.get("window", {}),
            "overview": {
                "Affected accounts": str(n),
                "Shared breach": sid,
                "Game / build": f"{game} / play_type {pt} / {tag}",
                "Operator": op,
                "Grain": "game/platform-level (distributed across accounts)"},
            "why_detected": why,
            "evidence_charts": [chart], "evidence_rounds": group_rounds,
            "ai_investigation": {
                "summary_md": (f"*Grouped platform-defect view — {n} accounts under {op} "
                               f"on {game} share the same {sid}. Drill into each account "
                               f"(Related, below) for its round-level case. This grouping "
                               f"is a routing aid: distribution across accounts points to a "
                               f"game/platform defect; a human decides.*"),
                "verified": [], "discrepancies": []},
            "explanations": [
                {"hypothesis": "Game/platform-level defect (e.g. wrong math sheet / cap not enforced)",
                 "plausibility": "high",
                 "note": "multiple accounts on one game/build share the identical breach"},
                {"hypothesis": f"{n} independent player exploits",
                 "plausibility": "low",
                 "note": "less likely given the shared game/build and identical breach"}],
            "recommended_actions": [
                {"action": "Investigate the game/build config (math sheet, cap enforcement)",
                 "owner": "platform / game-config", "urgency": "per severity"}],
            "status_history": [
                {"at": worst["detected_at"], "status": status, "by": "riskdet grouping",
                 "note": f"grouped {n} accounts sharing {sid} on {game}/{op}"}],
            "related": [m["id"] for m in members], "cohort_id": None,
            "disposition": None}

    def _make_buildwide_event(self, bwid, key, s, members) -> dict:
        day, tag, sid = key
        mev = [self._events[m] for m in members]
        sevrank = min(m["_sevrank"] for m in mev)
        sev = {0: "Critical", 1: "High", 2: "Medium", 3: "Low"}[sevrank]
        statuses = {m["status"] for m in mev}
        status = next((st for st in self._GROUP_STATUS_PRIORITY if st in statuses),
                      mev[0]["status"])
        n_ops, n_cells, n_acc = len(s["operators"]), len(s["cells"]), s["accounts"]
        label_en = _BREACH_LABEL.get(sid, ("integrity breach", "完整性破口"))[0]
        ops = sorted(s["per_op"], key=lambda o: -s["per_op"][o])
        chart = {"kind": "bars", "title": "Affected accounts per operator",
                 "note": "blast radius of one build defect across operators; each bar is an operator",
                 "labels": ops, "series": [s["per_op"][o] for o in ops], "ref": None,
                 "ylabel": "affected accounts", "suspect": [True] * len(ops),
                 "suspect_range": {"n": n_ops, "start": ops[0], "end": ops[-1]}}
        why = [{"signal": sid, "family": "INTEGRITY", "value": n_acc, "threshold": 0,
                "method": "shared across cells/operators on one build",
                "description": (f"The same absolute breach ({sid}) fires on build "
                                f"{tag} across {n_cells} cells / {n_ops} operators "
                                f"({n_acc} accounts) — the fingerprint of a build-wide "
                                f"platform defect: the build is defective wherever it is "
                                f"deployed, so this is ONE defect, not many.")}]
        # Aggregate round evidence across the whole build defect: cell-group
        # members already carry per-account rounds; singleton accounts contribute
        # their top round. Tagged per account, capped for readability.
        bw_rounds: list[dict] = []
        for m in mev:
            if m.get("grain") == "game_cell":
                bw_rounds.extend(m.get("evidence_rounds") or [])
            else:
                bw_rounds.extend(self._member_rounds([m], per_account=1))
        bw_rounds = bw_rounds[:24]
        return {
            "id": bwid, "case_id": bwid, "detected_at": max(m["detected_at"] for m in mev),
            "risk_type": "Game/Platform Defect", "severity": sev,
            "status": status, "_engine_status": status,
            "title": f"build {tag} {label_en} — {n_ops} operators / {n_cells} cells ({n_acc} accounts)",
            "operator": f"{n_ops} ops", "uid": "", "game_id": f"{n_cells} cells",
            "currency": "", "headline_metric": "", "_top": None, "_sevrank": sevrank,
            "grain": "build", "play_type": None, "sm_tag": tag,
            "_breaches": [sid],
            "_group": {"level": "build", "sid": sid, "n": n_acc, "n_ops": n_ops,
                       "n_cells": n_cells, "peak": s.get("peak"), "cap": s.get("cap"),
                       "members": members},
            "window": mev[0].get("window", {}),
            "overview": {
                "Affected accounts": str(n_acc),
                "Affected operators": str(n_ops),
                "Affected cells": str(n_cells),
                "Shared breach": sid,
                "Build (sm_tag)": tag,
                "Grain": "build-wide (cross-cell / cross-operator)"},
            "why_detected": why,
            "evidence_charts": [chart], "evidence_rounds": bw_rounds,
            "ai_investigation": {
                "summary_md": (f"*Build-wide platform-defect view — the same {sid} fires on "
                               f"build {tag} across {n_cells} cells / {n_ops} operators "
                               f"({n_acc} accounts). Drill into each operator/game group "
                               f"(Related, below), then into individual accounts. Distribution "
                               f"across builds/operators points to a build defect to fix at "
                               f"source; a human decides.*"),
                "verified": [], "discrepancies": []},
            "explanations": [
                {"hypothesis": "Build-wide platform defect (the build is defective wherever deployed)",
                 "plausibility": "high",
                 "note": "the identical breach spans multiple operators/games on one build"},
                {"hypothesis": "Coincidental independent per-cell issues",
                 "plausibility": "low",
                 "note": "unlikely to coincide across many operators on the same build"}],
            "recommended_actions": [
                {"action": f"Fix at source: audit build {tag} (math sheet / cap / settlement path)",
                 "owner": "platform engineering", "urgency": "per severity"}],
            "status_history": [
                {"at": max(m["detected_at"] for m in mev), "status": status,
                 "by": "riskdet grouping",
                 "note": f"build-wide rollup: {sid} across {n_ops} operators / {n_cells} cells"}],
            "related": members, "cohort_id": None, "disposition": None}

    def _readable_group_metric(self, e: dict, lang: str) -> str:
        g = e.get("_group") or {}
        zh = (lang == "zh")
        n = g.get("n", 0)
        peak = g.get("peak")

        def num(x):
            try:
                return f"{float(x):,.1f}".rstrip("0").rstrip(".")
            except (TypeError, ValueError):
                return str(x)
        sid = g.get("sid")
        if g.get("level") == "build":
            return (f"跨 {g.get('n_ops')} 個營運商／{g.get('n_cells')} 個儲存格，共 {n} 個帳號" if zh
                    else f"{g.get('n_ops')} operators / {g.get('n_cells')} cells, {n} accounts")
        if sid == "L1-MAX_X_BREACH":
            return (f"{n} 個帳號超上限（最高 {num(peak)}×）" if zh
                    else f"{n} accounts over cap (max {num(peak)}×)")
        if sid == "L1-BALANCE_IDENTITY":
            return (f"{n} 個帳號錢包帳務不符" if zh
                    else f"{n} accounts with wallet mismatches")
        if sid == "L1-DUP_ROUND":
            return (f"{n} 個帳號重複結算" if zh
                    else f"{n} accounts with duplicate settlement")
        return (f"{n} 個帳號共用完整性破口" if zh
                else f"{n} accounts share an integrity breach")

    def _risk_type(self, signals: list[dict]) -> tuple[str, dict]:
        best_type, best_sig = "Abnormal RTP", (signals[0] if signals else {})
        seen = {}
        for s in signals:
            rt = _SIGNAL_TYPE.get(s.get("signal_id")) or \
                _FAMILY_TYPE.get(s.get("family"), "Abnormal RTP")
            seen[rt] = s
        for rt in _TYPE_PRIORITY:
            if rt in seen:
                return rt, seen[rt]
        return best_type, best_sig

    def _case_file(self, case_id: str) -> pathlib.Path | None:
        p = self.root / "output" / "cases" / f"{case_id}.md"
        return p if p.is_file() else None

    @staticmethod
    def _risk_ts(c: dict) -> str:
        """When the risk actually happened, so an event is dated by its latest
        SUSPICIOUS EVIDENCE rather than the scan's as-of boundary. Preference:
        latest_evidence_utc (the flagged round's time) > max evidence_keys ts >
        last_active_utc (player's last play) > window_end fallback."""
        le = c.get("latest_evidence_utc")
        if le:
            return str(le)
        latest = ""
        for k in c.get("evidence_keys", []):
            _, _, ts = str(k).partition("@")
            if ts and ts > latest:
                latest = ts
        return latest or c.get("last_active_utc") or c.get("window_end", "")

    def _drilldown(self, case_id: str, day: str) -> dict | None:
        import os
        w1 = os.environ.get("RISKDET_DASHBOARD_SOURCE", "real").lower() == "w1"
        base = self.root / "out" / ("w1/drilldowns" if w1 else "drilldowns")
        for p in ((base / f"{case_id}@{day}.json") if day else None,
                  base / f"{case_id}.json"):
            if p and p.is_file():
                return json.loads(p.read_text(encoding="utf-8"))
        return None

    def _charts_for(self, c: dict, dd: dict | None) -> list[dict]:
        """Event-driven: one chart per fired family, chosen to *prove* that family.

        PRESCIENCE      -> split  (stake on triggering vs non-triggering rounds)
        OUTCOME_MAGNITUDE-> line  (daily RTP vs certified/peer reference)
        OUTCOME_FREQUENCY-> bars  (daily hit/trigger rate vs peer reference)
        INTEGRITY       -> bars   (max multiple vs certified cap | dup rounds |
                                   balance-identity violations, per signal)
        ROUTING         -> bars   (feature-buy share of turnover vs peer)
        TIMING          -> persec (rounds-per-second; only if cadence data present)
        A family is skipped silently if its drill-down series is unavailable.
        """
        if not dd:
            return []
        fams = set(c.get("families", []))
        sids = {s.get("signal_id") for s in c.get("signals", [])}
        L, S, R = dd.get("days", []), dd.get("series", {}), dd.get("ref", {})
        charts: list[dict] = []

        def has(key):
            return S.get(key) and any(v is not None for v in S[key])

        if ("OUTCOME_MAGNITUDE" in fams or "MAGNITUDE_WATCH" in fams) and has("rtp_pct"):
            src = R.get("rtp_pct_source", "reference")
            charts.append({"kind": "line", "title": "Daily RTP vs certified return",
                           "note": f"observed % return each day against the {src} "
                                   f"reference ({R.get('rtp_pct')}%)",
                           "labels": L, "series": S["rtp_pct"], "ref": R.get("rtp_pct"),
                           "ylabel": "RTP %"})
        if "PRESCIENCE" in fams and has("avg_bet_trigger"):
            charts.append({"kind": "split", "title": "Stake on triggering vs non-triggering rounds",
                           "note": "if outcomes were unknown these bars would match; "
                                   "a persistent gap is foreknowledge of the trigger",
                           "labels": L, "nontrigger": S["avg_bet_nontrigger"],
                           "trigger": S["avg_bet_trigger"],
                           "legend_a": "avg stake · non-trigger rounds",
                           "legend_b": "avg stake · trigger rounds",
                           "ylabel": "average stake"})
        if "OUTCOME_FREQUENCY" in fams:
            if "L1-HIT_RATE" in sids and has("hit_rate_pct"):
                charts.append({"kind": "bars", "title": "Daily win rate vs peer",
                               "note": f"share of rounds that win, vs peer {R.get('hit_rate_pct')}%",
                               "labels": L, "series": S["hit_rate_pct"],
                               "ref": R.get("hit_rate_pct"), "ylabel": "win rate %"})
            elif has("trigger_rate_pct"):
                charts.append({"kind": "bars", "title": "Daily feature-trigger rate vs peer",
                               "note": f"share of rounds that trigger the feature, vs peer "
                                       f"{R.get('trigger_rate_pct')}%",
                               "labels": L, "series": S["trigger_rate_pct"],
                               "ref": R.get("trigger_rate_pct"), "ylabel": "trigger rate %"})
        if "INTEGRITY" in fams:
            if "L1-MAX_X_BREACH" in sids and has("max_multiple"):
                charts.append({"kind": "bars", "title": "Max win multiple vs certified cap",
                               "note": f"a bar above the certified cap "
                                       f"({R.get('max_multiple_cap')}x) is impossible under honest math",
                               "labels": L, "series": S["max_multiple"],
                               "ref": R.get("max_multiple_cap"), "sqrt": True,
                               "ylabel": "win / bet (log)"})
            elif "L1-DUP_ROUND" in sids and has("dup_rounds"):
                charts.append({"kind": "bars", "title": "Duplicate-settled rounds per day",
                               "note": "same game_seq_id settled more than once; honest play is 0",
                               "labels": L, "series": S["dup_rounds"], "ref": 0,
                               "ylabel": "duplicate rounds"})
            elif "L1-BALANCE_IDENTITY" in sids and has("balance_violations"):
                charts.append({"kind": "bars", "title": "Balance-identity violations per day",
                               "note": "rounds where after != before - bet + win; honest play is 0",
                               "labels": L, "series": S["balance_violations"], "ref": 0,
                               "ylabel": "violations"})
        if "ROUTING" in fams and has("buy_share_pct"):
            charts.append({"kind": "bars", "title": "Feature-buy share of turnover vs peer",
                           "note": f"share of stake spent buying the feature, vs peer "
                                   f"{R.get('buy_share_pct')}%",
                           "labels": L, "series": S["buy_share_pct"],
                           "ref": R.get("buy_share_pct"), "ylabel": "buy share %"})
        if "TIMING" in fams and dd.get("persec"):
            charts.append({"kind": "persec", "title": "Rounds per second",
                           "note": "counts of seconds carrying N rounds; humans rarely exceed 1-2/s",
                           "mult": dd["persec"]})
        # Suspected-range demarcation: keep the full series but flag which points
        # fall in the suspected range (breach the reference) so a human reviewer
        # sees suspect vs normal in context. line/bars use the reference bound;
        # ref==0 rules (dup/balance violations) flag any positive value.
        for ch in charts:
            series = ch.get("series")
            if ch.get("kind") not in ("line", "bars") or series is None:
                continue
            ref = ch.get("ref")
            if ref is None:
                susp = [False] * len(series)
            elif ref == 0:
                susp = [bool(v is not None and v > 0) for v in series]
            else:
                susp = [bool(v is not None and v > ref) for v in series]
            ch["suspect"] = susp
            labels = ch.get("labels", [])
            flagged = [labels[i] for i, s in enumerate(susp) if s and i < len(labels)]
            if flagged:
                ch["suspect_range"] = {"n": len(flagged),
                                       "start": flagged[0], "end": flagged[-1]}
        return charts

    # Natural-language "Key metric" for the events table -- one short, readable
    # phrase per signal with its headline number, instead of the raw engine
    # string (e.g. "excess_turnover_z=8.364 vs frozen bound BH"). Bilingual.
    @staticmethod
    def _readable_metric(top: dict, lang: str = "en") -> str:
        zh = (lang == "zh")
        sid = top.get("signal_id") or ""
        v = top.get("value")
        eff = top.get("effect")
        thr = top.get("threshold")

        def num(x, d=1):
            try:
                return f"{float(x):,.{d}f}".rstrip("0").rstrip(".")
            except (TypeError, ValueError):
                return str(x)

        def plural(x, one, many):   # English pluralization for count phrases
            try:
                return one if int(float(x)) == 1 else many
            except (TypeError, ValueError):
                return many

        T = {
            "L1-MAX_X_BREACH": (f"賠付 {num(v)}× 超過上限 {num(thr)}×" if zh
                                else f"Paid {num(v)}× — over the {num(thr)}× cap"),
            "L1-BALANCE_IDENTITY": (f"{num(v,0)} 筆錢包帳務不符" if zh
                                    else f"{num(v,0)} wallet-balance {plural(v,'mismatch','mismatches')}"),
            "L1-DUP_ROUND": (f"{num(v,0)} 筆重複結算回合" if zh
                             else f"{num(v,0)} duplicate-settled {plural(v,'round','rounds')}"),
            "L1-EXCESS_TURNOVER": (f"RTP 高於認證 +{num(eff)} 個百分點" if zh and eff is not None
                                   else f"RTP +{num(eff)} pts above certified" if eff is not None
                                   else (f"回報遠高於認證 (z={num(v)})" if zh else f"Return far above certified (z={num(v)})")),
            "L1-PRESCIENT_BET": (f"下注可預測結果，大注押中 {num(eff)}×" if zh and eff is not None
                                 else f"Bets predict wins — {num(eff)}× bigger on wins" if eff is not None
                                 else (f"預知下注 (z={num(v)})" if zh else f"Bets predict outcomes (z={num(v)})")),
            "L1-TRIGGER_RATE": (f"觸發率為同儕 {num(eff)}×" if zh and eff is not None
                                else f"Feature-trigger rate {num(eff)}× peers" if eff is not None
                                else ("觸發率異常偏高" if zh else "Feature-trigger rate abnormally high")),
            "L1-HIT_RATE": (f"命中率為同儕 {num(eff)}×" if zh and eff is not None
                            else f"Win rate {num(eff)}× peers" if eff is not None
                            else ("命中率異常偏高" if zh else "Win rate abnormally high")),
            "L1-MAGNITUDE_WATCH": (f"RTP 為認證 {num(v)}×（低量觀察）" if zh
                                   else f"RTP {num(v)}× certified (low-volume watch)"),
            "L1-SAME_SECOND": ("同秒連續下注（疑似自動化）" if zh else "Same-second bursts (automation)"),
            "L1-RATE_CEILING": ("每分鐘下注數超上限" if zh else "Bets/min above human ceiling"),
            "L1-DUTY_CYCLE": ("近 24 小時不間斷（不像真人）" if zh else "Near 24/7 activity (non-human)"),
            "L2-OFF_TARGET_RTP": (f"整款遊戲 RTP 偏離認證 +{num(eff)} 個百分點" if zh and eff is not None
                                  else f"Game RTP +{num(eff)} pts off certified" if eff is not None
                                  else ("整款遊戲 RTP 偏離認證" if zh else "Game RTP off certified")),
            "L2-TRIGGER_DRIFT": (f"遊戲觸發率漂移 {num(eff)}×" if zh and eff is not None
                                 else f"Game trigger rate drifted {num(eff)}×" if eff is not None
                                 else ("遊戲觸發率漂移" if zh else "Game trigger-rate drift")),
            "L2-DEPLOY_SHIFT": (f"新版本 RTP 位移 {num(eff)} 個百分點" if zh
                                else f"New build shifted RTP {num(eff)} pts"),
            "L3-ML_DISCOVERY": ("行為離群（非監督模型）" if zh else "Behavioural outlier (ML)"),
            "L1-BETA_IN_PROD": ("測試版數學流入正式環境" if zh else "Beta math served in production"),
        }
        if sid in T:
            return T[sid]
        # fallback: humanised metric name + value
        name = (top.get("metric") or sid).replace("_", " ")
        return f"{name} = {num(v)}" if v is not None else name

    def _status(self, c: dict, case_md: str | None) -> tuple[str, list[dict]]:
        window_end = c.get("window_end", "")
        hist = [{"at": window_end, "status": "new", "by": "riskdet engine",
                 "note": f"escalation: {c['escalation']}"}]
        # A case file is an investigation triggered by human_review. On a day
        # where this candidate is only monitor-tier, that investigation had not
        # happened yet -- do not back-date the verdict onto the earlier day.
        if c["escalation"] != "human_review":
            return "monitoring", hist
        if case_md is None:
            # human_review AUTO-ENTERS the Layer-4 queue -- it is not left as a
            # loose 'new' item; it is queued for player-investigator/skeptic.
            hist.append({"at": window_end, "status": "queued", "by": "riskdet engine",
                         "note": "auto-routed to Layer 4 (human_review tier)"})
            return "queued", hist
        m = re.search(r"Verdict:\s*\**\s*(CONFIRMED|DOWNGRADED|REJECTED)", case_md, re.I)
        verdict = (m.group(1).upper() if m else None)
        status = {"CONFIRMED": "confirmed", "DOWNGRADED": "downgraded",
                  "REJECTED": "rejected"}.get(verdict, "skeptic_review")
        hist.append({"at": window_end, "status": status, "by": "skeptic",
                     "note": "Layer-4 review on file"})
        return status, hist

    def _to_event(self, c: dict) -> dict:
        signals = c.get("signals", [])
        rt, top = self._risk_type(signals)
        case_p = self._case_file(c["case_id"])
        case_md = case_p.read_text(encoding="utf-8") if case_p else None
        status, hist = self._status(c, case_md)
        val = top.get("value")
        # readable, natural-language key metric (bilingual at read time); keep the
        # raw engine string available for the detail page's technical fields.
        metric = self._readable_metric(top, "en")
        _top_min = {k: top.get(k) for k in
                    ("signal_id", "value", "threshold", "effect", "effect_unit", "metric")}
        sevrank = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}[c["severity"]]

        # evidence rounds from gsid@timestamp keys, with live retention status
        rounds = []
        for k in c.get("evidence_keys", [])[:10]:
            gsid, _, ts = k.partition("@")
            expired = True
            when = None
            if ts:
                try:
                    when = _dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
                    expired = (_now() - when) > _dt.timedelta(days=RETENTION_DAYS)
                except ValueError:
                    pass
            rounds.append({"gsid": gsid, "time": ts or "?", "note": "top evidence round",
                           "log_status": "expired (>30d retention)" if expired
                           else "available (in retention)"})

        overview = {
            "Signal families": f"{c['independent_families']} independent "
                               f"({', '.join(sorted(c['families']))})",
            "Escalation": c["escalation"],
            "Window (UTC)": f"{c.get('window_start','')[:10]} → {c.get('window_end','')[:10]}",
        }
        if c.get("expected_max_z") is not None:
            zs = [s["value"] for s in signals if s.get("p_value") is not None]
            if zs:
                overview["Peak z / expected null max"] = \
                    f"{max(zs):.2f} / {c['expected_max_z']:.2f} over 152,293 tested"
        if c.get("cohort_id"):
            overview["Cohort"] = f"{c['cohort_id']} (unapproved — dual-baseline reporting)"

        why = [{
            "signal": s.get("signal_id"), "family": s.get("family"),
            "value": round(s["value"], 3) if isinstance(s.get("value"), (int, float)) else s.get("value"),
            "threshold": s.get("threshold_method", ""),
            "method": s.get("baseline_source", ""),
            "description": s.get("description", ""),
        } for s in signals]

        # AI text: from the case file if present, else state 'not yet investigated'
        if case_md:
            ver = re.search(r"(Verdict:.*?)(?:\n\n|\Z)", case_md, re.S)
            ai = {"summary_md": (ver.group(1).strip()[:1200] if ver
                                 else "Case file on record; see output/cases/."),
                  "verified": [], "discrepancies": [
                      m.strip() for m in re.findall(r"[Dd]iscrepan\w+[:\-]\s*(.+)", case_md)][:4]}
        else:
            ai = {"summary_md": "*Not yet investigated — this candidate is in the "
                                "queue but no Layer-4 case file exists yet.*",
                  "verified": [], "discrepancies": []}

        scan_day = c.get("window_end", "")[:10]        # scan boundary (for drilldown lookup)
        # Production (real) dates events by latest suspicious evidence. The w1
        # SIMULATION demo keeps the original scan-day dating so prepared demo URLs
        # (e.g. .../P_gkkmmk_1049s_gk695782579@2026-08-10) stay stable.
        risk_ts = c.get("window_end", "") if self._w1_mode() else self._risk_ts(c)
        day = risk_ts[:10]                             # date the event by its activity, not the scan
        charts = self._charts_for(c, self._drilldown(c["case_id"], scan_day))
        ev = {
            "id": f"{c['case_id']}@{day}", "case_id": c["case_id"],
            "detected_at": risk_ts, "_scan_day": scan_day,
            "risk_type": rt, "severity": c["severity"], "status": status,
            "_engine_status": status,
            "title": f"{rt} — {c['uid']} @ {c['parent']} / {c['game_id']}",
            "operator": c["parent"], "uid": c["uid"], "game_id": c["game_id"],
            "currency": c.get("currency", ""), "headline_metric": metric,
            "_top": _top_min, "_sevrank": sevrank,
            "grain": c.get("grain"), "play_type": c.get("play_type"),
            "sm_tag": c.get("sm_tag"),
            "_breaches": sorted({s.get("signal_id") for s in signals
                                 if s.get("signal_id") in _BREACH_SIDS}),
            "window": {"start": c.get("window_start", ""), "end": c.get("window_end", "")},
            "overview": overview, "why_detected": why,
            "evidence_charts": charts, "evidence_rounds": rounds,
            "ai_investigation": ai,
            "explanations": [{"hypothesis": n, "plausibility": "medium", "note": ""}
                             for n in c.get("notes", [])] or
                            [{"hypothesis": "Requires Layer-4 investigation",
                              "plausibility": "medium",
                              "note": "no benign/adverse explanations assessed yet"}],
            "recommended_actions": [
                {"action": "Investigate via player-investigator",
                 "owner": "Layer-4", "urgency": "per severity SLA"}]
            if status in ("new", "queued", "skeptic_review") else
            [{"action": "Continue monitoring", "owner": "engine", "urgency": "weekly"}],
            "status_history": hist,
            "related": [], "cohort_id": c.get("cohort_id"),
            "disposition": None,
        }
        self._apply_disposition(ev)
        return ev

    # ------------------------------------------------------------------ #
    # Human review disposition -- the reviewer's recorded decision. The system
    # records it as an audit trail and reflects it in status/timeline; it never
    # acts on it (decision boundary: enforcement is 100% human).
    _DECISION_STATUS = {
        "dismiss_benign": "rejected", "keep_monitoring": "monitoring",
        "enhanced_monitoring": "monitoring", "needs_more_evidence": "investigating",
        "escalate_to_board": "confirmed", "confirmed_for_action": "confirmed"}
    _DECISION_LABEL = {
        "dismiss_benign": "Dismissed — benign", "keep_monitoring": "Keep monitoring",
        "enhanced_monitoring": "Enhanced monitoring",
        "needs_more_evidence": "Needs more evidence",
        "escalate_to_board": "Escalated to review board",
        "confirmed_for_action": "Confirmed for action"}

    def _dispo_dir(self):
        return self.root / "output" / "dispositions"

    def _load_dispositions(self) -> dict:
        out = {}
        d = self._dispo_dir()
        if d.is_dir():
            for p in d.glob("*.json"):
                try:
                    rec = json.loads(p.read_text(encoding="utf-8"))
                    if rec.get("event_id"):
                        out[rec["event_id"]] = rec
                except Exception:  # noqa: BLE001
                    pass
        return out

    def _w1_mode(self) -> bool:
        import os
        return os.environ.get("RISKDET_DASHBOARD_SOURCE", "real").lower() == "w1"

    def _alerts_for(self, date: str) -> list[dict]:
        """The de-duplicated alert queue for one scan day, from the state gate.

        These are ONLY the state transitions (opened / escalated / reopened) --
        i.e. what a human should actually see that day, not the full standing
        cumulative candidate list. Empty if the gate has not been run.
        """
        base = self.root / "out" / ("w1" if self._w1_mode() else "")
        p = base / f"alerts_{date}.jsonl"
        if not p.is_file():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    def alert_queue(self, date: str, lang: str = "en") -> list[dict]:
        """Deduped alerts for `date`, each linked to its event-detail page."""
        _ACT = {"opened": {"en": "opened", "zh": "新開案"},
                "escalated": {"en": "escalated", "zh": "升級"},
                "reopened": {"en": "reopened", "zh": "重啟"}}
        out = []
        for a in self._alerts_for(date):
            ev_id = f"{a['case_id']}@{date}"
            ev = self._events.get(ev_id)
            out.append({
                "event_id": ev_id if ev else None,
                "case_id": a["case_id"], "action": a["action"],
                "action_label": _ACT.get(a["action"], {}).get(lang, a["action"]),
                "escalation": a.get("escalation"), "severity": a.get("severity"),
                "families": a.get("families", []), "note": a.get("note", ""),
                "uid": a.get("uid"), "operator": a.get("parent"),
                "game_id": a.get("game_id"),
                "risk_type": (ev or {}).get("risk_type", "")})
        rank = {"reopened": 0, "escalated": 1, "opened": 2}
        out.sort(key=lambda x: (rank.get(x["action"], 9),
                                {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
                                .get(x["severity"], 4)))
        return out

    def _apply_disposition(self, ev: dict) -> None:
        """Idempotent: reset to the engine state, then overlay the latest human
        decision (if any) onto status + timeline + a disposition block."""
        ev["status_history"] = [h for h in ev.get("status_history", [])
                                if not h.get("_human")]
        ev["status"] = ev.get("_engine_status", ev["status"])
        ev["disposition"] = None
        rec = getattr(self, "_dispositions", {}).get(ev["id"])
        if not rec:
            return
        dec = rec.get("decision")
        ev["disposition"] = {
            "decision": dec, "decision_label": self._DECISION_LABEL.get(dec, dec),
            "reviewer": rec.get("reviewer"), "notes": rec.get("notes"),
            "at": rec.get("at")}
        ev["status"] = self._DECISION_STATUS.get(dec, ev["status"])
        ev["_sevrank"] = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}.get(
            ev["severity"], 3)
        note = f"human review: {self._DECISION_LABEL.get(dec, dec)}"
        if rec.get("notes"):
            note += f" — {rec['notes']}"
        ev["status_history"].append({
            "at": rec.get("at", ""), "status": ev["status"],
            "by": rec.get("reviewer", "human"), "note": note, "_human": True})

    def record_disposition(self, event_id: str, *, decision: str,
                           reviewer: str, notes: str) -> dict | None:
        ev = self._events.get(event_id)
        if ev is None:
            return None
        now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        prev = getattr(self, "_dispositions", {}).get(event_id)
        history = list(prev.get("history", [])) if prev else []
        if prev:
            history.append({k: prev.get(k) for k in
                            ("decision", "reviewer", "notes", "at")})
        rec = {"event_id": event_id, "case_id": ev.get("case_id"),
               "decision": decision, "reviewer": reviewer, "notes": notes,
               "at": now, "history": history}
        self._dispo_dir().mkdir(parents=True, exist_ok=True)
        fn = re.sub(r"[^A-Za-z0-9_.@-]", "_", event_id) + ".json"
        (self._dispo_dir() / fn).write_text(json.dumps(rec, indent=1), encoding="utf-8")
        self._dispositions[event_id] = rec
        self._apply_disposition(ev)
        self._order = sorted(self._events.values(),
                             key=lambda e: (e["detected_at"], e["_sevrank"]))
        return {k: v for k, v in ev.items() if not k.startswith("_")}

    # ------------------------------------------------------------------ #
    # bilingual section-title aliases and label maps (zh = Traditional Chinese)
    _SEC_EXEC = ("Executive Summary", "執行摘要")
    _SEC_CAVEATS = ("Data Caveats", "Caveats", "資料品質", "注意事項")
    _SEC_HUMAN = ("Human review", "人工複核", "Human-review")
    _SEC_OBS = ("Platform / game observations", "Platform/game observations",
                "平台 / 遊戲觀察", "平台/遊戲觀察", "平台")
    _SEC_AIREVIEW = ("AI review results", "AI 複核結果", "AI複核結果", "複核結果")
    _SEC_TODO = ("To-do", "To do", "待辦")
    _TOPIC_ZH = {"Calibration": "校準模型", "Game catalog": "遊戲資料表",
                 "Query cost": "查詢成本", "Data quality": "資料品質"}
    _SEV_ZH = {"WARN": "警告", "FAIL": "嚴重"}

    def _digest_body(self, date: str, lang: str = "en") -> str:
        """Composed digest markdown for `date`. When lang='zh', prefer the
        pre-generated Traditional-Chinese file, falling back to English."""
        rep = self.root / "output" / "reports"
        cands = ([rep / f"daily_digest_{date}.zh.md"] if lang == "zh" else []) \
            + [rep / f"daily_digest_{date}.md"]
        for p in cands:
            if p.is_file():
                return p.read_text(encoding="utf-8")
        return ""

    @staticmethod
    def _caveat_topic(text: str) -> str:
        """Map a caveat line/filename to a stable canonical topic (no date)."""
        low = text.lower()
        if "calibrat" in low:
            return "Calibration"
        if "catalog" in low:
            return "Game catalog"
        if "cost" in low or "ledger" in low:
            return "Query cost"
        b = re.search(r"\*\*(.+?)\*\*", text)
        fn = re.search(r"([A-Za-z_]+?)_\d{4}-\d{2}-\d{2}", text)
        topic = b.group(1) if b else (fn.group(1) if fn else text)
        return re.sub(r"[*`]", "", topic).strip().rstrip(".:")[:40] or "Data quality"

    def _caveats_for(self, date: str, lang: str = "en") -> list[str]:
        """Data-quality caveats for the selected cycle, sourced from the
        structured data_quality/*.md 'Verdict:' lines (reliable) and collapsed
        to stable 'Topic — WARN/FAIL' labels, localized to `lang`. The artifact's
        filename date is intentionally dropped: these are global/frozen quality
        artifacts that govern every replay cycle equally, so surfacing a
        future-dated filename (e.g. 08-19) against an 08-01 selection misled.
        The per-cycle narrative and file citations remain in the digest body."""
        found: dict[str, str] = {}   # canonical topic -> WARN|FAIL (FAIL outranks)
        for p in sorted((self.root / "output" / "data_quality").glob("*.md")):
            m = re.search(r"Verdict:\s*\**\s*(WARN|FAIL)", p.read_text(encoding="utf-8"))
            if not m:
                continue
            topic, sev = self._caveat_topic(p.stem), m.group(1)
            if topic not in found or (sev == "FAIL" and found[topic] == "WARN"):
                found[topic] = sev
        if lang == "zh":
            return [f"{self._TOPIC_ZH.get(k, k)} — {self._SEV_ZH.get(v, v)}"
                    for k, v in found.items()]
        return [f"{k} — {v}" for k, v in found.items()]

    def daily_summary(self, date: str, lang: str = "en") -> dict:
        evs = [e for e in self._order if e["detected_at"][:10] == date]
        by_sev = {s: sum(1 for e in evs if e["severity"] == s) for s in SEVERITIES}
        by_type: dict[str, int] = {}
        for e in evs:
            by_type[e["risk_type"]] = by_type.get(e["risk_type"], 0) + 1
        caveats = self._caveats_for(date, lang)
        # State-gate contrast: `events` is the full standing cumulative queue for
        # the day; `alerts` is what the dedup gate says is actually NEW/changed.
        alerts = self._alerts_for(date)
        abd = {"opened": 0, "escalated": 0, "reopened": 0}
        for a in alerts:
            abd[a["action"]] = abd.get(a["action"], 0) + 1
        return {"date": date,
                "totals": {"events": len(evs),
                           "new": sum(1 for e in evs if e["status"] == "new"),
                           "critical": by_sev["Critical"], "high": by_sev["High"],
                           "confirmed": sum(1 for e in evs if e["status"] == "confirmed"),
                           "downgraded": sum(1 for e in evs if e["status"] == "downgraded"),
                           "alerts": len(alerts), "standing": len(evs),
                           "opened": abd["opened"], "escalated": abd["escalated"],
                           "reopened": abd["reopened"]},
                "by_severity": by_sev, "by_type": by_type,
                "players_screened": 152293, "signals": len(self._order),
                "queries_cost_usd": 0.0, "data_caveats": caveats}

    @staticmethod
    def _section(body: str, *titles: str) -> str:
        """Return the text under the first matching '## <title>' up to the next '## '."""
        lines = body.splitlines()
        for i, ln in enumerate(lines):
            h = ln.lstrip("#").strip().lower()
            if ln.startswith("#") and any(h.startswith(t.lower()) for t in titles):
                out = []
                for nxt in lines[i + 1:]:
                    if nxt.startswith("## "):
                        break
                    out.append(nxt)
                return "\n".join(out).strip()
        return ""

    def daily_digest(self, date: str, lang: str = "en") -> dict:
        zh = (lang == "zh")
        # display body for THIS date only (zh -> en fallback happens inside the
        # helper). Do NOT fall back to another date's report -- showing 08-03's
        # digest on the 08-10 page is misleading; be explicit when none exists.
        body = self._digest_body(date, lang)
        # as-of cutoff for THIS date (cumulative end-of-day), not the global
        # window max -- the digest is per-day, so its timestamp must track the
        # selected date, not always the last day loaded.
        as_of = f"{date}T16:00:00"
        if not body:
            return {"date": date, "generated_at": as_of,
                    "headline": (f"{date} 尚無彙整摘要。" if zh
                                 else f"No digest composed for {date}."),
                    "body_md": (f"本日（{date}）尚未執行第 4 層 report-composer 產生摘要；"
                                "偵測結果請見下方風險事件表。" if zh
                                else f"No Layer-4 digest has been composed for {date} yet. "
                                "See the Risk Events table below for this day's detections."),
                    "look_first": ("請參閱下方風險事件表。" if zh
                                   else "See the Risk Events table below."),
                    "decision_items": []}

        strip_md = lambda s: re.sub(r"[*`]", "", s)   # emphasis only; keep _ in ids

        # headline: prefer the handoff note's "Bottom line:" one-liner; fall back
        # to the first Executive Summary line (old digest format).
        m = re.search(r"(?im)^\s*[*_`]*\s*(?:Bottom line|結論)\s*[*_`]*\s*[:：]\s*(.+)$", body)
        if m:
            headline = strip_md(m.group(1)).strip()
        else:
            def _headline_line(l):
                return re.sub(r"[*`]", "", l).lstrip("-–—*0123456789. ").strip()
            summ = self._section(body, *self._SEC_EXEC)
            headline = next((_headline_line(l)
                             for l in summ.splitlines()
                             if l.strip() and not l.lstrip().startswith(("|", "#"))
                             and set(l.strip()) != {"-"}),
                            "已彙整之每日摘要。" if zh else "Composed daily digest on file.")

        # look-first: prefer the handoff note's "Data quality:" one-liner; fall
        # back to the first Data Caveats bullet (old format).
        m2 = re.search(r"(?im)^\s*[*_`]*\s*(?:Data quality|資料品質)\s*[*_`]*\s*[:：]\s*(.+)$", body)
        if m2:
            look = strip_md(m2.group(1)).strip()
        else:
            caveats = self._section(body, *self._SEC_CAVEATS)
            look = next((strip_md(l).lstrip("-* ").strip()
                         for l in caveats.splitlines() if l.strip().startswith(("-", "*", "1."))),
                        "") or ("本週期無阻斷性資料品質注意事項。" if zh
                                else "No blocking data-quality caveats this cycle.")

        # decision items: parse the "Human review — act on these" table (handoff
        # format); fall back to any row containing 'human_review' (old format).
        # case_ids/families are language-neutral, so parse the English body.
        en_body = self._digest_body(date, "en") or body
        fam_tokens = ("OUTCOME", "PRESCIENCE", "TIMING", "INTEGRITY", "ROUTING", "ENVIRONMENT")
        _cols = lambda l: [c.strip() for c in l.strip().strip("|").split("|")]
        decisions = []
        for l in self._section(en_body, *self._SEC_HUMAN).splitlines():
            if not l.strip().startswith("|"):
                continue
            cols = _cols(l)
            if len(cols) < 4 or cols[0].lower() in ("case", "") or set("".join(cols)) <= set("-: "):
                continue
            case = cols[0].strip("`")
            fams = next((c for c in cols if any(tk in c for tk in fam_tokens)), "")
            sla = next((c for c in cols
                        if any(s in c for s in ("1h", "same-day", "48h", "weekly", "SLA", "·"))), "")
            decisions.append(" — ".join(x for x in (case, fams, sla) if x))
        if not decisions:                               # old-format fallback
            suffix = "人工複核（當日 SLA）" if zh else "human_review (same-day SLA)"
            for l in en_body.splitlines():
                if l.startswith("|") and "human_review" in l:
                    cols = _cols(l)
                    if len(cols) >= 5 and cols[0].lower() not in ("case id", ""):
                        fams = next((c for c in cols if any(tk in c for tk in fam_tokens)), "")
                        decisions.append(f"{cols[0]} — {fams} — {suffix}")
        seen, dedup = set(), []
        for d in decisions:
            if d not in seen:
                seen.add(d); dedup.append(d)

        # Stats one-liner, and short bullet/numbered lists (wrapped lines joined)
        # for the structured box. The box renders THESE, never the full body_md
        # or the appendix -- so no file names/paths or raw evidence reach it.
        ms = re.search(r"(?im)^\s*[*_`]*\s*(?:Stats|統計)\s*[*_`]*\s*[:：]\s*(.+)$", body)
        stats = strip_md(ms.group(1)).strip() if ms else ""

        def _items(titles, n):
            items, cur = [], None
            for l in self._section(body, *titles).splitlines():
                if re.match(r"^\s*([-*]|\d+\.)\s+", l):
                    if cur is not None:
                        items.append(cur)
                    cur = re.sub(r"^\s*([-*]|\d+\.)\s+", "", l).strip()
                elif cur is not None and l.strip():
                    cur += " " + l.strip()
            if cur is not None:
                items.append(cur)
            out = [strip_md(x).strip() for x in items if strip_md(x).strip()]
            return [x for x in out if x.lower().rstrip(".—- ") not in ("none", "")][:n]

        return {"date": date,
                "generated_at": as_of,
                "headline": headline,          # Bottom line
                "stats": stats,                # Stats one-liner
                "look_first": look,            # Data quality
                "decision_items": dedup[:5],   # Top human_review cases
                "observations": _items(self._SEC_OBS, 3),
                "ai_review": _items(self._SEC_AIREVIEW, 3),
                "todos": _items(self._SEC_TODO, 3),
                "body_md": body}               # kept for other consumers; box ignores it

    @staticmethod
    def _base_case(case_id: str) -> str:
        """Day-less case identity: strip a trailing @YYYY-MM-DD (group ids carry
        the day; player case_ids do not). Used to collapse the same case appearing
        across multiple days into one row."""
        return re.sub(r"@\d{4}-\d{2}-\d{2}$", "", case_id or "")

    def list_events(self, *, date_from=None, date_to=None, severity=None,
                    risk_type=None, status=None, operator=None, q=None,
                    actionable=False, limit=200, lang="en",
                    collapse_days=True) -> list[dict]:
        # 1) collect all matches (ascending detected_at, from self._order)
        matches = []
        for e in self._order:
            d = e["detected_at"][:10]
            if date_from and d < date_from: continue
            if date_to and d > date_to: continue
            # actionable = the human_review tier; monitor-tier is routine backlog
            if actionable and e["status"] == "monitoring": continue
            if severity and e["severity"] != severity: continue
            if risk_type and e["risk_type"] != risk_type: continue
            if status and e["status"] != status: continue
            if operator and e["operator"] != operator: continue
            if q and q.lower() not in f"{e['id']} {e['title']} {e['uid']} {e['operator']} {e['game_id']}".lower():
                continue
            matches.append(e)

        # 2) collapse the SAME case across days into one row (its lifecycle) --
        #    a date-range search should show one case, not one row per day. The
        #    latest day's row represents it; occurrences/first/last give the span.
        if collapse_days:
            agg: dict[str, dict] = {}
            for e in matches:                       # ascending detected_at
                b = self._base_case(e["case_id"])
                s = agg.get(b)
                if s is None:
                    agg[b] = {"latest": e, "first": e["detected_at"][:10],
                              "last": e["detected_at"][:10], "n": 1}
                else:
                    s["n"] += 1
                    s["last"] = e["detected_at"][:10]
                    if e["detected_at"] >= s["latest"]["detected_at"]:
                        s["latest"] = e
            picked = list(agg.values())
            picked.sort(key=lambda s: s["last"], reverse=True)          # recent first
            sev = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
            picked.sort(key=lambda s: sev.get(s["latest"]["severity"], 3))  # severity primary
            items = [(s["latest"], s["n"], s["first"], s["last"]) for s in picked]
        else:
            items = [(e, 1, e["detected_at"][:10], e["detected_at"][:10]) for e in matches]

        # 3) build rows
        out = []
        for e, n, first, last in items:
            row = {k: e[k] for k in ("id", "detected_at", "risk_type",
                   "severity", "status", "title", "operator", "uid",
                   "game_id", "headline_metric")}
            row["occurrences"] = n
            row["first_seen"] = first
            row["last_seen"] = last
            if e.get("_group"):
                row["headline_metric"] = self._readable_group_metric(e, lang)
            elif e.get("_top"):
                row["headline_metric"] = self._readable_metric(e["_top"], lang)
            out.append(row)
            if len(out) >= limit: break
        return out

    # ------------------------------------------------------------------ #
    # event-detail localisation (zh). The digests are pre-translated; the event
    # page's free text (title, signal descriptions, verdict, explanations) comes
    # from the engine/case-file in English, so localise it here on read.
    _RISK_TYPE_ZH = {
        "Abnormal RTP": "異常 RTP", "Advantage Play": "優勢玩家",
        "Feature Buy Abuse": "功能購買濫用", "Suspicious Betting Pattern": "可疑下注模式",
        "Bot/Automation": "機器人／自動化", "Game RTP Shift": "遊戲 RTP 位移",
        "Settlement Anomaly": "結算異常", "Balance Reconciliation": "錢包對帳",
        "API Anomaly": "API 異常", "ML Anomaly": "ML 異常",
        "Game/Platform Defect": "遊戲／平台缺陷"}

    def _sig_desc_zh(self, w: dict) -> str:
        sid = w.get("signal") or ""
        v = w.get("value")
        vv = f"{v:.1f}" if isinstance(v, (int, float)) else v
        t = {
            "L1-PRESCIENT_BET": f"觸發回合的下注額顯著偏高（z={vv}）：在誠實數學下，回合結果與該回合所選下注額無關 —— 下注可預測結果即為預知（prescience）徵兆。",
            "L1-EXCESS_TURNOVER": f"相對認證 RTP 的超額回報 z={vv}：贏分遠高於此玩法認證應付水準。",
            "L1-TRIGGER_RATE": f"功能觸發率相對同儕異常偏高（z={vv}，二項檢定 + BH-FDR）。",
            "L1-HIT_RATE": f"命中率相對同儕異常偏高（z={vv}，二項檢定 + BH-FDR）。",
            "L1-MAX_X_BREACH": f"單筆賠付倍數 {vv}× 超過認證上限 —— 誠實數學下不可能發生。",
            "L1-DUP_ROUND": "同一 game_seq_id 重複結算 —— 雙重結算或 ETL 重複，需 Cloud Logging 佐證。",
            "L1-BALANCE_IDENTITY": f"{vv} 筆回合的錢包帳務不符（after ≠ before − bet + win）。",
            "L1-MAGNITUDE_WATCH": f"低量但金額顯著的高回報（RTP 比率 {vv}×）—— 觀察名單，非顯著性檢定。",
            "L2-OFF_TARGET_RTP": f"遊戲儲存格單日 RTP 偏離認證值 +{vv} RTP 點（遊戲層級漂移）。",
            "L2-DEPLOY_SHIFT": "新版本整體 RTP 相對前一版本位移（部署回歸）。",
            "L2-TRIGGER_DRIFT": f"遊戲儲存格觸發率相對其他日漂移（z={vv}）。",
            "L3-ML_DISCOVERY": "非監督模型（Isolation Forest）判定行為離群 —— 「異常」不等於「有風險」，須調查方具意義。",
        }
        return t.get(sid, w.get("description", ""))

    def _localize_event(self, e: dict, lang: str) -> dict:
        ev = {k: v for k, v in e.items() if not k.startswith("_")}
        if lang != "zh":
            return ev
        rt_zh = self._RISK_TYPE_ZH.get(ev.get("risk_type"), ev.get("risk_type"))
        g = e.get("_group")
        if g and g.get("level") == "build":
            breach_zh = _BREACH_LABEL.get(g["sid"], ("", "完整性破口"))[1]
            ev["title"] = (f"版本 {ev['sm_tag']} {breach_zh} — "
                           f"{g['n_ops']} 個營運商／{g['n_cells']} 個儲存格（{g['n']} 個帳號）")
            _ovk = {"Affected accounts": "受影響帳號", "Affected operators": "受影響營運商",
                    "Affected cells": "受影響儲存格", "Shared breach": "共用破口",
                    "Build (sm_tag)": "版本 (sm_tag)", "Grain": "粒度"}
            ev["overview"] = {_ovk.get(k, k): ("版本層級（跨儲存格／跨營運商）"
                              if k == "Grain" else v)
                              for k, v in ev.get("overview", {}).items()}
            ev["why_detected"] = [{**w, "description":
                (f"同一絕對破口（{w.get('signal')}）在版本 {ev['sm_tag']} 上跨 "
                 f"{g['n_cells']} 個儲存格／{g['n_ops']} 個營運商（{g['n']} 個帳號）出現"
                 f"——這是版本層級平台缺陷的特徵：該版本部署到哪裡就壞到哪裡，"
                 f"因此是「一個缺陷」，不是許多個。應從源頭修正版本。")}
                for w in ev.get("why_detected", [])]
        elif g:
            # cell-level grouped platform-defect case: bespoke zh title / overview / why
            breach_zh = _BREACH_LABEL.get(g["sid"], ("", "完整性破口"))[1]
            ev["title"] = f"{ev['game_id']} {breach_zh} — {g['n']} 個帳號 @ {ev['operator']}"
            _ovk = {"Affected accounts": "受影響帳號", "Shared breach": "共用破口",
                    "Game / build": "遊戲 / 版本", "Operator": "營運商", "Grain": "粒度"}
            ev["overview"] = {_ovk.get(k, k): ("遊戲／平台層級（跨多個帳號分布）"
                              if k == "Grain" else v)
                              for k, v in ev.get("overview", {}).items()}
            ev["why_detected"] = [{**w, "description":
                (f"{ev['operator']} 於遊戲 {ev['game_id']} 有 {g['n']} 個帳號共用同一絕對破口"
                 f"（{w.get('signal')}）——單一遊戲上跨多帳號的破口指向平台／遊戲層級缺陷，"
                 f"而非 {g['n']} 個各自獨立的玩家。")}
                for w in ev.get("why_detected", [])]
        else:
            ev["title"] = f"{rt_zh} — {ev['uid']} @ {ev['operator']} / {ev['game_id']}"
            ev["why_detected"] = [{**w, "description": self._sig_desc_zh(w)}
                                  for w in ev.get("why_detected", [])]
        # localize evidence chart titles / axis labels / legends / notes
        ct = {"Daily RTP vs certified return": "每日 RTP 對比認證回報",
              "Stake on triggering vs non-triggering rounds": "觸發 vs 非觸發回合的下注額",
              "Daily feature-trigger rate vs peer": "每日功能觸發率對比同儕",
              "Daily win rate vs peer": "每日命中率對比同儕",
              "Max win multiple vs certified cap": "最大贏分倍數對比認證上限",
              "Duplicate-settled rounds per day": "每日重複結算回合數",
              "Balance-identity violations per day": "每日錢包帳務不符筆數",
              "Feature-buy share of turnover vs peer": "功能購買佔投注比例對比同儕",
              "Rounds per second": "每秒回合數",
              "Per-account breach vs certified reference": "各帳號破口對比認證基準",
              "Affected accounts per operator": "各營運商受影響帳號數"}
        yl = {"RTP %": "RTP %", "average stake": "平均下注額", "trigger rate %": "觸發率 %",
              "win rate %": "命中率 %", "win / bet (log)": "贏分/下注（對數）",
              "duplicate rounds": "重複回合數", "violations": "違規筆數", "buy share %": "購買比例 %"}
        lg = {"avg stake · non-trigger rounds": "平均下注 · 非觸發回合",
              "avg stake · trigger rounds": "平均下注 · 觸發回合"}

        def _note_zh(s):
            if not s:
                return s
            reps = [("observed % return each day against the", "每日觀察回報率，對比"),
                    ("reference", "基準"),
                    ("if outcomes were unknown these bars would match; a persistent gap is "
                     "foreknowledge of the trigger",
                     "若結果未知，兩組應相等；持續的落差即為對觸發的預知"),
                    ("share of rounds that trigger the feature, vs peer", "觸發功能的回合比例，對比同儕"),
                    ("share of rounds that win, vs peer", "命中回合比例，對比同儕"),
                    ("a bar above the certified cap", "超過認證上限的長條"),
                    ("is impossible under honest math", "在誠實數學下不可能"),
                    ("same game_seq_id settled more than once; honest play is 0",
                     "同一 game_seq_id 重複結算；誠實情況應為 0"),
                    ("rounds where after != before - bet + win; honest play is 0",
                     "after ≠ before − bet + win 的回合；誠實情況應為 0"),
                    ("share of stake spent buying the feature, vs peer", "購買功能所佔投注比例，對比同儕"),
                    ("counts of seconds carrying N rounds; humans rarely exceed 1-2/s",
                     "每秒承載 N 回合的秒數統計；人類鮮少超過 1–2/秒"),
                    ("each bar is one account's peak on this game; all breach the reference",
                     "每根長條為一個帳號在此遊戲的峰值；全部超過基準"),
                    ("blast radius of one build defect across operators; each bar is an operator",
                     "單一版本缺陷跨營運商的影響範圍；每根長條為一個營運商"),
                    ("certified", "認證"), ("peer", "同儕")]
            for a, b in reps:
                s = s.replace(a, b)
            return s
        for c in ev.get("evidence_charts", []):
            c["title"] = ct.get(c.get("title"), c.get("title"))
            if c.get("ylabel"):
                c["ylabel"] = yl.get(c["ylabel"], c["ylabel"])
            for k in ("legend_a", "legend_b"):
                if c.get(k):
                    c[k] = lg.get(c[k], c[k])
            c["note"] = _note_zh(c.get("note"))
        # AI verdict: prefer a zh case file if present
        zc = self.root / "output" / "cases" / f"{ev['case_id']}.zh.md"
        if zc.is_file():
            md = zc.read_text(encoding="utf-8")
            ver = re.search(r"(Verdict:.*?)(?:\n\n|\Z)", md, re.S)
            m2 = re.search(r"(判定[:：].*?)(?:\n\n|\Z)", md, re.S)
            ev["ai_investigation"] = {**ev.get("ai_investigation", {}),
                "summary_md": ((m2.group(1) if m2 else ver.group(1).strip()) if (m2 or ver)
                               else md[:1200])}
        elif "*Not yet investigated" in (ev.get("ai_investigation", {}) or {}).get("summary_md", ""):
            ev["ai_investigation"] = {**ev["ai_investigation"],
                "summary_md": "*尚未調查 —— 此候選在佇列中，但尚無第 4 層案件檔。*"}
        ev["explanations"] = [{**x,
            "hypothesis": ("需第 4 層調查" if x.get("hypothesis") == "Requires Layer-4 investigation"
                           else x.get("hypothesis")),
            "note": ("尚未評估良性／不利解釋" if "no benign/adverse" in (x.get("note") or "")
                     else x.get("note"))} for x in ev.get("explanations", [])]
        act_zh = {"Investigate via player-investigator": "由 player-investigator 進行調查",
                  "Continue monitoring": "持續監控", "Layer-4": "第 4 層",
                  "engine": "引擎", "per severity SLA": "依嚴重度 SLA", "weekly": "每週"}
        ev["recommended_actions"] = [{
            "action": act_zh.get(a.get("action"), a.get("action")),
            "owner": act_zh.get(a.get("owner"), a.get("owner")),
            "urgency": act_zh.get(a.get("urgency"), a.get("urgency"))}
            for a in ev.get("recommended_actions", [])]
        for r in ev.get("evidence_rounds", []):
            if r.get("note") == "top evidence round":
                r["note"] = "重點證據回合"
            if isinstance(r.get("log_status"), str):
                r["log_status"] = (r["log_status"]
                                   .replace("expired (>30d retention)", "已逾期（>30 天保留）")
                                   .replace("available (in retention)", "可取得（保留期內）"))
        if ev.get("disposition"):
            ev["disposition"]["decision_label"] = {
                "dismiss_benign": "駁回 — 良性", "keep_monitoring": "維持監控",
                "enhanced_monitoring": "加強監控", "needs_more_evidence": "需更多證據",
                "escalate_to_board": "上報審查委員會", "confirmed_for_action": "確認需處置",
            }.get(ev["disposition"].get("decision"), ev["disposition"].get("decision_label"))
        return ev

    # ------------------------------------------------------------------ #
    # Deterministic auto-report -- generated from ENGINE DATA only (no LLM
    # agent). Every human_review case gets one, so a reviewer always has a
    # detailed, reproducible report with the suspected-range data, even before
    # (or without) a Layer-4 agent investigation. This is NOT a verdict.
    def _build_auto_report(self, ev: dict, lang: str) -> str:
        """Compact suspected-range summary -- the ONE thing not shown elsewhere on
        the page (signals, evidence rounds and follow-up already have their own
        sections). Returns '' when there is no suspected range to add, so the card
        stays hidden rather than repeating the page."""
        zh = (lang == "zh")
        charts = [ch for ch in ev.get("evidence_charts", []) if ch.get("suspect_range")]
        if not charts:
            return ""
        title = "疑似範圍（超出基準的資料點）" if zh else "Suspected range (points beyond the reference)"
        npts = (lambda n: f"{n} 點") if zh else (lambda n: f"{n} pt" + ("" if n == 1 else "s"))
        refw = "基準" if zh else "ref"
        sep = "、" if zh else ", "
        out = [f"## {title}", ""]
        for ch in charts:
            sr = ch["suspect_range"]
            susp = ch.get("suspect") or []
            labels = ch.get("labels") or []
            series = ch.get("series") or []
            span = sr["start"] if sr["start"] == sr["end"] else f"{sr['start']} → {sr['end']}"
            refv = ch.get("ref")
            refp = (f"（{refw} {refv}）" if zh else f" ({refw} {refv})") if refv is not None else ""
            pts = [f"{labels[i]}={series[i]}" for i in range(len(susp))
                   if susp[i] and i < len(labels) and i < len(series)]
            shown = sep.join(pts[:8])
            if len(pts) > 8:
                shown += (f"… 等 {len(pts)} 點" if zh else f"… +{len(pts) - 8} more")
            tail = f"： *{shown}*" if (zh and shown) else (f": *{shown}*" if shown else "")
            out.append(f"- **{ch.get('title','')}** — {npts(sr['n'])}（{span}）{refp}{tail}"
                       if zh else
                       f"- **{ch.get('title','')}** — {npts(sr['n'])} ({span}){refp}{tail}")
        return "\n".join(out)

    def get_event(self, event_id: str, lang: str = "en") -> dict | None:
        e = self._events.get(event_id)
        if e is None:
            return None
        ev = self._localize_event(e, lang)
        ev["auto_report_md"] = self._build_auto_report(ev, lang)
        return ev

    def stats(self, *, date_from: str, date_to: str) -> dict:
        evs = [e for e in self._order if date_from <= e["detected_at"][:10] <= date_to]
        daily: dict[str, dict] = {}
        d0, d1 = _dt.date.fromisoformat(date_from), _dt.date.fromisoformat(date_to)
        d = d0
        while d <= d1:
            daily[str(d)] = {"date": str(d), "events": 0, "critical_high": 0}
            d += _dt.timedelta(days=1)
        by_type = {t: 0 for t in RISK_TYPES}
        by_sev = {s: 0 for s in SEVERITIES}
        by_status: dict[str, int] = {}
        by_op: dict[str, int] = {}
        for e in evs:
            day = e["detected_at"][:10]
            if day in daily:
                daily[day]["events"] += 1
                if e["severity"] in ("Critical", "High"):
                    daily[day]["critical_high"] += 1
            by_type[e["risk_type"]] += 1
            by_sev[e["severity"]] += 1
            by_status[e["status"]] = by_status.get(e["status"], 0) + 1
            by_op[e["operator"]] = by_op.get(e["operator"], 0) + 1
        confirmed = by_status.get("confirmed", 0)
        resolved = confirmed + sum(by_status.get(k, 0) for k in ("rejected", "downgraded", "closed"))
        return {"daily": list(daily.values()), "by_type": by_type,
                "by_severity": by_sev, "by_status": by_status,
                "by_operator": sorted(({"operator": k, "count": v} for k, v in by_op.items()),
                                      key=lambda r: -r["count"])[:10],
                "mtti_hours": 0.0,
                "confirm_rate": round(confirmed / resolved, 3) if resolved else 0.0}
