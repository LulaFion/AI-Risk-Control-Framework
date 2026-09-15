"""MockProvider — deterministic mock data shaped exactly like the real system.

Seeded RNG => stable ids and lists across restarts. Three flagship events
mirror the shapes of real investigated cases (prescient-bet collapse, bot
burst with max-win stop, dual-signal with wallet gap) so the detail page is
exercised with realistic depth; the rest are generated across the nine risk
types, ~90 days, several operators.

Swap for a real provider without touching the UI (see base.py).
"""

from __future__ import annotations

import datetime as dt
import random
from .base import RISK_TYPES, SEVERITIES, DataProvider

TODAY = dt.date(2026, 8, 20)
OPERATORS = ["mlcafecny", "jdbygmmk", "gkkmmk", "jdbkkmmk", "ssbet77php",
             "shwe666mmk", "rpg666thb", "wofa168cny", "ing501mmk", "jdbkxmmk",
             "v3cnys_mb", "zzagphp"]
GAMES = ["1049s", "1018s", "1045s", "1028s", "1008s", "1011s", "1050s",
         "1035s", "1025s", "1057s", "1019s"]
CCY = {"mlcafecny": "CNY", "wofa168cny": "CNY", "v3cnys_mb": "CNY",
       "ssbet77php": "PHP", "zzagphp": "PHP", "rpg666thb": "THB"}

_STATUS_FLOW = ["new", "investigating", "skeptic_review",
                "downgraded", "monitoring", "closed", "confirmed", "rejected"]


def _mk_generic(rng: random.Random, i: int, day: dt.date) -> dict:
    rt = rng.choice(RISK_TYPES)
    sev = rng.choices(SEVERITIES, weights=[4, 10, 30, 56])[0]
    op = rng.choice(OPERATORS)
    game = rng.choice(GAMES)
    uid = f"{op[:2]}{rng.randrange(10**7, 10**8)}"
    age = (TODAY - day).days
    if age <= 1:
        st = rng.choice(["new", "new", "investigating"])
    elif age <= 5:
        st = rng.choice(["investigating", "skeptic_review", "monitoring", "downgraded"])
    else:
        st = rng.choices(["closed", "downgraded", "monitoring", "rejected", "confirmed"],
                         weights=[40, 25, 20, 10, 5])[0]
    z = round(rng.uniform(4.5, 14.0), 2)
    metric = {
        "Abnormal RTP": f"RTP {rng.uniform(1.1, 2.4):.2f} vs cert 0.966",
        "Advantage Play": f"stake lift {rng.uniform(1.8, 3.2):.2f}x on triggers",
        "Feature Buy Abuse": f"buy share {rng.uniform(0.55, 0.97):.0%} of turnover",
        "Game RTP Shift": f"day +{rng.uniform(4, 22):.1f} RTP pts vs cert",
        "Suspicious Betting Pattern": f"bet ramp {rng.uniform(2.2, 6.0):.1f}x pre-win",
        "Bot/Automation": f"{rng.randrange(130, 240)} rounds/min peak",
        "Settlement Anomaly": f"{rng.randrange(1, 4)} duplicate settlement(s)",
        "Balance Reconciliation": f"identity gap {rng.choice([+50, +100, -20, +200]):+.2f}",
        "API Anomaly": f"resCode!=1000 rate {rng.uniform(0.5, 4.0):.1f}%",
    }[rt]
    eid = f"EV-{day:%Y%m%d}-{i:03d}"
    return {
        "id": eid, "detected_at": f"{day}T{rng.randrange(0,24):02d}:{rng.randrange(0,60):02d}:00Z",
        "risk_type": rt, "severity": sev, "status": st,
        "title": f"{rt} — {uid} @ {op} / {game}",
        "operator": op, "uid": uid, "game_id": game,
        "currency": CCY.get(op, "MMK"), "headline_metric": metric,
        "z": z,
    }


def _detail_for(ev: dict, rng: random.Random) -> dict:
    """Generic detail synthesis for non-flagship events."""
    rt = ev["risk_type"]
    days = [(dt.date.fromisoformat(ev["detected_at"][:10])
             - dt.timedelta(days=k)) for k in range(11, -1, -1)]
    base = [round(max(0.0, rng.gauss(0.96, 0.06)), 3) for _ in days]
    spike_idx = rng.randrange(6, 12)
    series = base[:]
    if rt in ("Abnormal RTP", "Game RTP Shift", "Advantage Play"):
        series[spike_idx] = round(rng.uniform(1.6, 3.2), 3)
    charts = [{
        "kind": "line", "title": "Daily RTP vs certified",
        "note": "Certified baseline 0.966 (dashed). One-day deviation drives the signal.",
        "labels": [f"{d:%m-%d}" for d in days], "series": series, "ref": 0.966,
    }]
    if rt == "Bot/Automation":
        charts = [{
            "kind": "bars", "title": "Rounds per minute",
            "note": "Physical plausibility bound 120/min (dashed).",
            "labels": [f"m{k}" for k in range(1, 13)],
            "series": [rng.randrange(20, 60) for _ in range(8)]
                      + [rng.randrange(125, 200) for _ in range(4)],
            "ref": 120,
        }]
    why = [{
        "signal": {"Abnormal RTP": "L1-EXCESS_TURNOVER", "Advantage Play": "L1-PRESCIENT_BET",
                   "Feature Buy Abuse": "L1-FEATURE_ROUTING", "Game RTP Shift": "L2-OFF_TARGET_RTP",
                   "Suspicious Betting Pattern": "L1-BET_STRUCTURE", "Bot/Automation": "L1-RATE_CEILING",
                   "Settlement Anomaly": "L1-DUP_ROUND", "Balance Reconciliation": "L1-BALANCE_IDENTITY",
                   "API Anomaly": "L4-LOG_PATTERN"}[rt],
        "family": {"Abnormal RTP": "OUTCOME_MAGNITUDE", "Advantage Play": "PRESCIENCE",
                   "Feature Buy Abuse": "ROUTING", "Game RTP Shift": "OUTCOME_MAGNITUDE",
                   "Suspicious Betting Pattern": "ROUTING", "Bot/Automation": "TIMING",
                   "Settlement Anomaly": "INTEGRITY", "Balance Reconciliation": "INTEGRITY",
                   "API Anomaly": "INTEGRITY"}[rt],
        "value": ev["z"], "threshold": "BH-FDR q=0.01 + effect floor",
        "method": "frozen calibration (fit May, validated Jun–Jul)",
        "description": ev["headline_metric"],
    }]
    st = ev["status"]
    hist = [{"at": ev["detected_at"], "status": "new", "by": "riskdet engine",
             "note": "emitted by detection scan"}]
    if st != "new":
        hist.append({"at": ev["detected_at"][:11] + "09:00:00Z", "status": st,
                     "by": "skeptic" if st in ("downgraded", "rejected", "confirmed")
                           else "analyst", "note": "mock lifecycle step"})
    return {
        **ev,
        "window": {"start": f"{days[0]}", "end": f"{days[-1]}"},
        "overview": {"Rounds in window": rng.randrange(800, 60000),
                     "Turnover": f"{rng.randrange(500, 90000):,} {ev['currency']}",
                     "Net win": f"{rng.randrange(-5000, 30000):+,} {ev['currency']}",
                     "Peak z-score": ev["z"],
                     "Population screened": "152,293 players",
                     "Expected null max z": 4.4},
        "why_detected": why,
        "evidence_charts": charts,
        "evidence_rounds": [
            {"gsid": f"{rng.randrange(16**8):08x}-mock-round-{k}",
             "time": ev["detected_at"], "note": f"top win #{k+1}",
             "log_status": "available" if rng.random() > 0.4
                           else "expired (>30d retention)"} for k in range(3)],
        "ai_investigation": {
            "summary_md": f"**Mock AI investigation.** The {rt} signal on "
                          f"`{ev['uid']}` was screened against the peer cell "
                          f"({ev['game_id']}/pt0/{ev['currency']}). Key checks: "
                          f"single-spike test, leave-operator-out baseline, "
                          f"persistence split-half. *(Replace via provider with "
                          f"real Layer-4 investigator output.)*",
            "verified": ["Engine numbers recomputed from artifacts — match",
                         "Peer baseline excludes player and operator"],
            "discrepancies": [] if rng.random() > 0.4 else
                             ["Claimed window off by one day vs artifacts"]},
        "explanations": [
            {"hypothesis": "Genuine variance (lucky streak)", "plausibility": "high",
             "note": "passes unless persistence holds in both half-windows"},
            {"hypothesis": "Client autoplay / turbo", "plausibility": "medium",
             "note": "1-second timestamps cannot distinguish bot vs turbo alone"},
            {"hypothesis": "Coordinated cohort membership", "plausibility": "low",
             "note": "check out/proposed_exclusions.yaml fingerprints"},
        ],
        "recommended_actions": [
            {"action": "Continue monitoring", "owner": "engine", "urgency": "weekly"},
            {"action": "Pull round logs while inside 30-day retention",
             "owner": "player-investigator", "urgency": "this week"},
        ],
        "status_history": hist,
        "related": [],
    }


# --------------------------------------------------------------------------- #
# Flagship events — mirror the three real investigated cases
# --------------------------------------------------------------------------- #
def _flagships() -> list[dict]:
    f1 = {  # prescient-bet collapse
        "id": "EV-20260819-001",
        "detected_at": "2026-08-19T01:38:00Z",
        "risk_type": "Advantage Play", "severity": "Medium", "status": "downgraded",
        "title": "Advantage Play — intable5 @ ing501mmk / 1045s",
        "operator": "ing501mmk", "uid": "intable5", "game_id": "1045s",
        "currency": "MMK", "headline_metric": "stake lift 2.15x on triggers (z=8.44)",
        "z": 8.44,
        "window": {"start": "2026-04-30", "end": "2026-05-14"},
        "overview": {"Rounds": 4937, "Triggers": "56 (1.13%)",
                     "Turnover": "3,869.8 MMK", "Win": "5,636.26 MMK",
                     "RTP": "145.7% vs certified 96.66%",
                     "Account net across 12 games": "+232.3 MMK (breakeven)"},
        "why_detected": [
            {"signal": "L1-PRESCIENT_BET", "family": "PRESCIENCE", "value": 8.44,
             "threshold": "permutation null + BH q=0.01",
             "method": "exact permutation from sufficient statistics",
             "description": "stake on triggering rounds 1.686 vs overall 0.784 (2.15x)"},
            {"signal": "L1-EXCESS_TURNOVER", "family": "OUTCOME_MAGNITUDE", "value": 4.29,
             "threshold": "BH q=0.01 + 3 RTP-pt floor",
             "method": "certified baseline, unequal-stake SE",
             "description": "+49.0 RTP points over certified"}],
        "evidence_charts": [{
            "kind": "split", "title": "Stake distribution: trigger vs non-trigger",
            "note": "The only 50-stake round in the account's history — and it triggered "
                    "(win 1,862.5 = 33% of all winnings). Distributions match otherwise.",
            "labels": ["0.4", "0.6", "1.0", "2.0", "3.0", "5.0", "50"],
            "trigger": [16.1, 32.1, 46.4, 3.6, 0, 0, 1.8],
            "nontrigger": [23.3, 36.3, 34.8, 5.5, 0.04, 0.06, 0]}],
        "evidence_rounds": [
            {"gsid": "c6cdc4a6-1b7a-4a99-9665-13328b400425", "time": "2026-05-04T09:xx",
             "note": "the bet-50 trigger round (win 1,862.5)",
             "log_status": "expired (>30d retention) — permanently un-investigable"}],
        "ai_investigation": {
            "summary_md": "**Skeptic verdict: DOWNGRADED to Low / watchlist (gate HOLD).** "
                          "Both fired families collapse to a single 2026-05-04 round — "
                          "removing it: RTP 1.4565 → 0.9879, z 4.291 → 0.239, lift 2.151 → "
                          "1.043. “One event counted twice, not two independent lines of "
                          "evidence.” Full-sample z=4.29 is BELOW the expected null max "
                          "(4.36) over 152,293 players.",
            "verified": ["All engine numbers reproduced exactly from artifacts",
                         "Account is net breakeven across 12 games (+232.3 MMK)"],
            "discrepancies": [
                "Claimed cohort membership unsupported — intable5 is NOT in "
                "cohort_d49d8dd4fbf1 (lists intable00001–00009)",
                "Last activity 2026-05-14, not 05-13 (logs unavailable either way)"]},
        "explanations": [
            {"hypothesis": "Single lucky max-stake round (variance)", "plausibility": "high",
             "note": "P(the one 50-stake round triggers) ≈ trigger rate 1.13%"},
            {"hypothesis": "Foreknowledge / RNG information", "plausibility": "low",
             "note": "would not explain net-breakeven play across 12 games"},
            {"hypothesis": "Internal test account (naming adjacency)", "plausibility": "medium",
             "note": "requires ownership confirmation; NOT evidence as artifacts stand"}],
        "recommended_actions": [
            {"action": "Watchlist the uid pattern; escalate if it reappears while logs fresh",
             "owner": "engine", "urgency": "weekly"},
            {"action": "Confirm ing501mmk 'intable*' ownership with platform team",
             "owner": "human risk control", "urgency": "this week"}],
        "status_history": [
            {"at": "2026-08-19T01:38Z", "status": "new", "by": "riskdet engine",
             "note": "human_review escalation (2 families)"},
            {"at": "2026-08-19T15:00Z", "status": "investigating", "by": "player-investigator",
             "note": "drill-down + collapse test"},
            {"at": "2026-08-20T10:00Z", "status": "downgraded", "by": "skeptic",
             "note": "DOWNGRADED to Low; gate HOLD"}],
        "related": ["EV-20260819-002", "EV-20260819-003"],
    }
    f2 = {  # bot burst, max-win stop
        "id": "EV-20260819-002",
        "detected_at": "2026-08-19T01:38:00Z",
        "risk_type": "Bot/Automation", "severity": "Medium", "status": "monitoring",
        "title": "Bot/Automation — kx4iq1ui2xsn @ jdbkxmmk / 1028s",
        "operator": "jdbkxmmk", "uid": "kx4iq1ui2xsn", "game_id": "1028s",
        "currency": "MMK", "headline_metric": "822 rounds in 8m35s; 4/sec peak",
        "z": 7.58,
        "window": {"start": "2026-05-30", "end": "2026-05-30"},
        "overview": {"Lifetime": "822 rounds in 8m 35s (single session, single game)",
                     "Peak cadence": "4 settled rounds/second (61 such seconds)",
                     "Turnover": "860.5 MMK", "Net win": "+939.5 MMK",
                     "RTP": "209.2% → 106.1% without the one 888x round",
                     "Ending": "hit certified max win 888x, stopped 5s later, never returned"},
        "why_detected": [
            {"signal": "L1-SAME_SECOND", "family": "TIMING", "value": 0.596,
             "threshold": "frozen bound 0.561", "method": "ROBUST (median+N·MAD, frozen)",
             "description": "59.6% of gaps are same-second"},
            {"signal": "L1-RATE_CEILING", "family": "TIMING", "value": 141,
             "threshold": "frozen bound 126.7", "method": "ROBUST",
             "description": "141 rounds in the peak minute"},
            {"signal": "L1-EXCESS_TURNOVER", "family": "OUTCOME_MAGNITUDE", "value": 7.58,
             "threshold": "BH q=0.01 + 3pt floor", "method": "certified baseline",
             "description": "+112.6 RTP points (rejected by skeptic: single 888x round)"}],
        "evidence_charts": [{
            "kind": "persec", "title": "Rounds settled per second (entire lifetime)",
            "note": "Three-phase script shape; orange = the 888x max-win second at t=510; "
                    "activity stops 5 seconds later.",
            "mult": {"1": 80, "2": 78, "3": 114, "4": 61}}],
        "evidence_rounds": [
            {"gsid": "(888x round)", "time": "2026-05-30T09:30:03Z",
             "note": "max win = certified cap, correct payout",
             "log_status": "expired (>30d retention)"}],
        "ai_investigation": {
            "summary_md": "**Skeptic verdict: DOWNGRADED to Low — automation-capability "
                          "watch flag (timing-only); gate HOLD.** TIMING survived exact "
                          "recomputation (489/821 same-second gaps; 61 four-round seconds "
                          "— exceeds any known client turbo). Money family rejected: one "
                          "legal cap-hit; z without it = 0.64.",
            "verified": ["Multiplicity table exact: 80/78/114/61",
                         "Single-cell lifetime confirmed across three snapshots"],
            "discrepancies": ["Span 8m35s not 8m36s; residual z 0.64 not ~0.35 (both null)"]},
        "explanations": [
            {"hypothesis": "Scripted probe with stop-at-max-win condition", "plausibility": "high",
             "note": "capability is the risk signal; money trivial (~USD 0.5)"},
            {"hypothesis": "Human supervising a bot, cashed out at peak", "plausibility": "medium",
             "note": "equally consistent with the 5s stop"},
            {"hypothesis": "Client turbo/autoplay", "plausibility": "low",
             "note": "4 settled rounds/sec sustained exceeds known turbo (~2/sec)"}],
        "recommended_actions": [
            {"action": "Cadence-signature watch on jdbkxmmk / 1028s (3-4 rounds/sec at min stakes)",
             "owner": "engine", "urgency": "standing"},
            {"action": "Escalate immediately if pattern reappears (logs will be fresh)",
             "owner": "player-investigator", "urgency": "conditional"}],
        "status_history": [
            {"at": "2026-08-19T01:38Z", "status": "new", "by": "riskdet engine",
             "note": "human_review escalation"},
            {"at": "2026-08-20T10:00Z", "status": "monitoring", "by": "skeptic",
             "note": "DOWNGRADED to Low; timing-only watch flag"}],
        "related": ["EV-20260819-001"],
    }
    f3 = {  # dual-signal + wallet gap
        "id": "EV-20260819-003",
        "detected_at": "2026-08-19T01:38:00Z",
        "risk_type": "Balance Reconciliation", "severity": "High", "status": "skeptic_review",
        "title": "Balance Reconciliation — m1n000000004 @ v3cnys_mb / 1049s (+1018s excess)",
        "operator": "v3cnys_mb", "uid": "m1n000000004", "game_id": "1049s",
        "currency": "CNY", "headline_metric": "identity gap +100.00 exact; 1018s RTP 476%",
        "z": 8.15,
        "window": {"start": "2026-04-30", "end": "2026-08-15"},
        "overview": {"1018s cell": "1,287 rounds / 13 days, RTP 476.6% vs cert 96.46%",
                     "Spike": "one bet-4.0 round won 4,867 (1216.75x, within 5000x cap) = 79% of winnings",
                     "Wallet event": "+100.00 exact gap, 2026-05-15, 1 of 230,475 rounds",
                     "Account scale": "230,475 rounds on 1049s alone",
                     "Log verification": "4 in-retention rounds clean (single settlement, resCode 1000)"},
        "why_detected": [
            {"signal": "L1-EXCESS_TURNOVER", "family": "OUTCOME_MAGNITUDE", "value": 8.15,
             "threshold": "BH q=0.01 + 3pt floor", "method": "certified baseline",
             "description": "+380.1 RTP points on 1018s (n=1,287)"},
            {"signal": "L1-BALANCE_IDENTITY", "family": "INTEGRITY", "value": 1,
             "threshold": "absolute (after = before - bet + win)", "method": "same-row identity",
             "description": "one violation on 1049s: gap exactly +100.00"}],
        "evidence_charts": [{
            "kind": "bars", "title": "1018s daily winnings (sqrt scale)",
            "note": "June 8 alone is 92.8% of everything this cell ever paid.",
            "labels": ["04-30","05-26","05-30","06-08","06-11","06-15","07-09",
                        "07-27","07-31","08-05","08-07","08-09","08-15"],
            "series": [4.76,0.6,63.9,5681.8,18.9,12.84,6.62,12.56,126.1,0,8.3,0.26,188.2],
            "sqrt": True}],
        "evidence_rounds": [
            {"gsid": "caf2db09-f662-4d5d-9ad2-d6af882227a2", "time": "2026-05-15T20:13:03Z",
             "note": "the +100.00 identity-gap round",
             "log_status": "expired (>30d retention)"},
            {"gsid": "39f9a0b7-504e-4a00-9d88-fd40f93e1e19", "time": "2026-08-15T01:26:22Z",
             "note": "1018s round, log-verified clean", "log_status": "verified: 1 settlement, resCode 1000"}],
        "ai_investigation": {
            "summary_md": "**Skeptic verdict: DOWNGRADED to Low; player money signal "
                          "REJECTED; gate HOLD.** The 4,867 spike round itself was never "
                          "log-verified and is now past retention. Leave-player-out parent "
                          "RTP is 0.9962 — peers run above certified on this title. New "
                          "innocent explanation for the +100.00: a concurrent-settlement "
                          "race on a shared wallet (player provably plays 1018s and 1049s "
                          "in overlapping windows). Identity gap forwarded as a conditional "
                          "**platform/wallet** integrity follow-up.",
            "verified": ["All headline arithmetic reproduced exactly",
                         "4 in-retention rounds: single settlement, resCode 1000"],
            "discrepancies": [
                "“4 rounds log-verified” was misleading — 3 of 4 are 1049s neighbors; "
                "the spike round was never verified",
                "Excluding the spike day, remaining 12 days run at RTP 0.628 — below certified"]},
        "explanations": [
            {"hypothesis": "Internal test account (v3cnys_* family)", "plausibility": "high",
             "note": "naming + 230k-round scale match the proposed cohort family; ownership unconfirmed"},
            {"hypothesis": "Concurrent-settlement race on shared wallet", "plausibility": "medium",
             "note": "skeptic-identified; explains an exact +100.00 without misconduct"},
            {"hypothesis": "Out-of-band manual credit colliding with a round", "plausibility": "medium",
             "note": "operator wallet ledger would settle it"}],
        "recommended_actions": [
            {"action": "Confirm v3cnys_mb ownership — the disposition fork",
             "owner": "human risk control", "urgency": "this week"},
            {"action": "If NOT internal: escalate wallet event to Critical platform integrity",
             "owner": "platform team", "urgency": "conditional"},
            {"action": "Check operator wallet ledger & bonus logs around 2026-05-15 20:13 UTC",
             "owner": "query-analyst", "urgency": "this week"}],
        "status_history": [
            {"at": "2026-08-19T01:38Z", "status": "new", "by": "riskdet engine",
             "note": "human_review escalation, severity High"},
            {"at": "2026-08-20T10:00Z", "status": "skeptic_review", "by": "skeptic",
             "note": "review complete; awaiting ownership confirmation to close"}],
        "related": ["EV-20260819-001"],
    }
    return [f1, f2, f3]


class MockProvider(DataProvider):
    def __init__(self) -> None:
        rng = random.Random(20260820)
        self._events: dict[str, dict] = {}
        rows: list[dict] = []
        # 90 days of generated events
        for back in range(0, 90):
            day = TODAY - dt.timedelta(days=back)
            n = rng.choices([0, 1, 1, 2, 2, 3, 4], weights=[10, 25, 25, 18, 12, 7, 3])[0]
            for i in range(n):
                ev = _mk_generic(rng, i + 1, day)
                rows.append(ev)
        for ev in rows:
            self._events[ev["id"]] = _detail_for(ev, rng)
        for f in _flagships():
            self._events[f["id"]] = f
        self._order = sorted(self._events.values(),
                             key=lambda e: e["detected_at"], reverse=True)

    # ------------------------------------------------------------------ #
    def daily_summary(self, date: str) -> dict:
        evs = [e for e in self._order if e["detected_at"][:10] == date]
        by_sev = {s: 0 for s in SEVERITIES}
        by_type: dict[str, int] = {}
        for e in evs:
            by_sev[e["severity"]] += 1
            by_type[e["risk_type"]] = by_type.get(e["risk_type"], 0) + 1
        return {
            "date": date,
            "totals": {"events": len(evs),
                       "new": sum(1 for e in evs if e["status"] == "new"),
                       "critical": by_sev["Critical"], "high": by_sev["High"],
                       "confirmed": sum(1 for e in evs if e["status"] == "confirmed"),
                       "downgraded": sum(1 for e in evs if e["status"] == "downgraded")},
            "by_severity": by_sev, "by_type": by_type,
            "players_screened": 152293, "signals": 1452,
            "queries_cost_usd": 1.36,
            "data_caveats": [
                "Calibration WARN (2026-08-19): 3 timing/turnover rules over the 0.1% capacity ceiling in specific operators",
                "Catalog WARN: games 1003m/1058s/6801r/6901g have no certified RTP — NOT TESTED ≠ clean",
            ],
        }

    def daily_digest(self, date: str, lang: str = "en") -> dict:
        if lang == "zh":
            return {
                "date": date, "generated_at": f"{date}T10:00:00Z",
                "headline": "零確認案;3 件人工覆核案經質疑者審查後全數降級;"
                            "6 個協同帳號群提案待歸屬確認。",
                "body_md": (
                    "**視窗 2026-04-30 → 2026-08-17(UTC 事件時間)。** 篩查 152,293 "
                    "名玩家,1,452 個訊號 → 1,203 個候選 → 3 件人工覆核。\n\n"
                    "三件覆核案經對抗式審查後全數**降級為低**——每個金錢訊號都死於"
                    "單峰檢定(各是一次合法大獎)。存留項目:一面自動化能力觀察旗"
                    "(每秒 4 局結算)與一筆 **+100.00 錢包恆等式缺口**,後者以"
                    "「平台完整性追蹤事項」有條件轉送。\n\n"
                    "積壓:1,200 件監控層候選(特定營運商的時序訊號量部分屬校準容量"
                    "假影)。另 1019s 有一局賠付 3333.33× 超過 3333× 上限——"
                    "極可能是精度問題,值得一條查詢。"),
                "look_first": "確認 v3cnys_* 系列(尤其 v3cnys_mb)的歸屬——一個答案"
                              "同時處置唯一原 High 案與全部 6 個協同群提案(103 帳號)。",
                "decision_items": [
                    "協同群歸屬確認(v3cnys_*、ing501mmk intable*)",
                    "金錢升級加入絕對投注額底線(3/3 案皆為微額單峰)",
                    "ML 驗證結果未寫入 data_quality/ —— 管線缺口"],
            }
        return {
            "date": date, "generated_at": f"{date}T10:00:00Z",
            "headline": "Zero confirmed findings; 3 human-review cases skeptic-reviewed "
                        "and downgraded; 6 coordinated-cohort proposals await ownership "
                        "confirmation.",
            "body_md": (
                "**Window 2026-04-30 → 2026-08-17 (UTC event time).** 152,293 players "
                "screened, 1,452 signals → 1,203 candidates → 3 human-review.\n\n"
                "All three reviewed cases were **downgraded to Low** after adversarial "
                "review — every money signal failed the single-spike test (one legal "
                "jackpot each). Surviving items: an automation-capability watch flag "
                "(4 settled rounds/second) and a **+100.00 wallet identity gap** routed "
                "as a conditional platform integrity follow-up.\n\n"
                "Backlog: 1,200 monitor-tier candidates (timing volume partly a "
                "calibration-capacity artifact in flagged operators). One round on 1019s "
                "paid 3333.33× vs the 3333× cap — likely precision, worth one query."),
            "look_first": "Confirm ownership of the v3cnys_* parents (esp. v3cnys_mb) — "
                          "one answer disposes the only originally-High case and all six "
                          "cohort proposals (103 uids).",
            "decision_items": [
                "Cohort ownership confirmation (v3cnys_*, ing501mmk intable*)",
                "Absolute-turnover floor for money escalations (3/3 cases were single-spike dust)",
                "ML validation verdict not written to data_quality/ — pipeline gap"],
        }

    def list_events(self, *, date_from=None, date_to=None, severity=None,
                    risk_type=None, status=None, operator=None, q=None,
                    limit=100) -> list[dict]:
        out = []
        for e in self._order:
            d = e["detected_at"][:10]
            if date_from and d < date_from:
                continue
            if date_to and d > date_to:
                continue
            if severity and e["severity"] != severity:
                continue
            if risk_type and e["risk_type"] != risk_type:
                continue
            if status and e["status"] != status:
                continue
            if operator and e["operator"] != operator:
                continue
            if q:
                blob = f"{e['id']} {e['title']} {e['uid']} {e['operator']} {e['game_id']}".lower()
                if q.lower() not in blob:
                    continue
            out.append({k: e[k] for k in
                        ("id", "detected_at", "risk_type", "severity", "status",
                         "title", "operator", "uid", "game_id", "headline_metric")})
            if len(out) >= limit:
                break
        return out

    def get_event(self, event_id: str) -> dict | None:
        return self._events.get(event_id)

    def stats(self, *, date_from: str, date_to: str) -> dict:
        evs = [e for e in self._order
               if date_from <= e["detected_at"][:10] <= date_to]
        daily: dict[str, dict] = {}
        d0, d1 = dt.date.fromisoformat(date_from), dt.date.fromisoformat(date_to)
        d = d0
        while d <= d1:
            daily[str(d)] = {"date": str(d), "events": 0, "critical_high": 0}
            d += dt.timedelta(days=1)
        by_type: dict[str, int] = {t: 0 for t in RISK_TYPES}
        by_sev = {s: 0 for s in SEVERITIES}
        by_status: dict[str, int] = {}
        by_op: dict[str, int] = {}
        for e in evs:
            day = e["detected_at"][:10]
            daily[day]["events"] += 1
            if e["severity"] in ("Critical", "High"):
                daily[day]["critical_high"] += 1
            by_type[e["risk_type"]] += 1
            by_sev[e["severity"]] += 1
            by_status[e["status"]] = by_status.get(e["status"], 0) + 1
            by_op[e["operator"]] = by_op.get(e["operator"], 0) + 1
        confirmed = by_status.get("confirmed", 0)
        resolved = confirmed + by_status.get("rejected", 0) + by_status.get("downgraded", 0) \
                   + by_status.get("closed", 0)
        return {
            "daily": list(daily.values()),
            "by_type": by_type, "by_severity": by_sev, "by_status": by_status,
            "by_operator": sorted(({"operator": k, "count": v} for k, v in by_op.items()),
                                  key=lambda r: -r["count"])[:10],
            "mtti_hours": 26.4,
            "confirm_rate": round(confirmed / resolved, 3) if resolved else 0.0,
        }
