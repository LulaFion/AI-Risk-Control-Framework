"""Generate synthetic risk injections A01 / A09 / A23 for REVIEW.

Produces local files only -- NOTHING is written to BigQuery here. After you
review, a separate step MERGEs the chosen CSVs into a sim copy of the archive.

STEALTH BY DESIGN. The attackers must blend in, or the detection test is
dishonest -- an obvious `sim_*` uid hands the engine a free hint and would also
trip the uid-skeleton cohort detector for the wrong reason. So:
  uid     mimics each operator's real pattern (yg…, 1041ml…) -- looks real
  gsid    ordinary uuid4-style -- looks real
  sm_tag  the REAL build tag -- it is a peer-cell key; a marker here would leak
  balance identity holds on every row -- so none falsely trips L1-BALANCE_IDENTITY

Removability rides on a channel the detection logic never inspects:
  serial >= 9_100_000_000   (real max is ~2.1e9)   <-- the surgical cleanup key
The manifest records every attacker's exact uid, serial range and gsid list,
which is BOTH the removal ledger AND the ground truth for scoring the replay.

Collision caveat: a stealth uid could coincide with a real account and pollute
its sim stats. The paid-step plan therefore verifies non-collision before MERGE
(see manifest). Nothing here touches production.

Rows match acp-develop.OMG_riskdet.rounds_all exactly (18 columns).
Outputs: out/sim/inject_A0x.csv, out/sim/manifest.md, out/sim/ground_truth.json,
         out/logs_sim/<case>/<gsid>.json (simulated:true).
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import pathlib
import random
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "out" / "sim"
LOGS = ROOT / "out" / "logs_sim"
COLS = ["game_time", "report_date", "serial", "game_seq_id", "parent", "uid",
        "game_id", "play_type", "currency", "sm_v", "sm_tag", "bet",
        "valid_bet", "win", "before_balance", "after_balance", "triggered",
        "last_modify_time"]

CERT = {"1049s": {0: (0.9690, 5000)}, "1018s": {0: (0.9646, 5000)},
        "1045s": {0: (0.9666, 2000), 2: (0.9667, 2000)}}

# real sm_tag observed live for these games -- used verbatim (no marker)
SM_TAG = "0.2.2.20260102"
DAY = _dt.date(2026, 8, 1)
REPORT = _dt.date(2026, 8, 1)
SERIAL_BASE = 9_100_000_000            # THE removal key: serial >= this == synthetic

_RNG = random.Random(0xA77AC5)


def stealth_uid(operator: str) -> str:
    """A uid that mimics the operator's real naming so it blends in."""
    if operator == "jdbygmmk":            # real: ygks888, yg2288999, yg4kp2u0hxr6
        body = "".join(_RNG.choice("0123456789abcdefghjkmnpqrstuvwxyz") for _ in range(8))
        return f"yg{body}"
    if operator == "mlcafecny":           # real: 1041ml5, 1041ml16
        return f"1041ml{_RNG.randrange(700, 990)}"
    body = "".join(_RNG.choice("0123456789") for _ in range(9))
    return f"{operator[:2]}{body}"


def gsid() -> str:
    return str(uuid.UUID(int=_RNG.getrandbits(128), version=4))


def _ts(sec: int, day: _dt.date = DAY) -> _dt.datetime:
    return _dt.datetime(day.year, day.month, day.day, 12, 0, 0) + _dt.timedelta(seconds=sec)


def row(serial, g, uid, parent, game, ptype, ccy, bet, win, before, triggered, t):
    after = round(before - bet + win, 5)
    return {
        # report_date = UTC+8 business date; at noon UTC it equals the event date
        "game_time": t.strftime("%Y-%m-%dT%H:%M:%S"), "report_date": t.date().isoformat(),
        "serial": serial, "game_seq_id": g, "parent": parent, "uid": uid,
        "game_id": game, "play_type": ptype, "currency": ccy,
        "sm_v": "feature_base", "sm_tag": SM_TAG, "bet": round(bet, 5),
        "valid_bet": round(bet, 5), "win": round(win, 5),
        "before_balance": round(before, 5), "after_balance": after,
        "triggered": triggered,
        "last_modify_time": (t + _dt.timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S"),
    }, after


def spinlog(g, game, ptype, ccy, bet, win, before, t, res_count=1, res_code=1000):
    """Fixture shaped EXACTLY like a real scripts/pull_logs.py evidence file
    (case_id, game_seq_id, round_time_utc, window_minutes, filter, identity,
    entries) with no synthetic marker -- so Layer-4 investigation is blind.
    Provenance/cleanup rides on the out/logs_sim/ directory isolation and the
    gsid ledger in ground_truth.json, not on any in-file flag."""
    after = round(before - bet + win, 5)
    entries = [{
        "timestamp": (t + _dt.timedelta(milliseconds=40 * i)).isoformat() + "+00:00",
        "container": "spin-server", "pod": f"spin-server-{55 + i}cc97bc58-5v8vc",
        "severity": "INFO",
        "payload": (f'Info Spin-Server Response info - {{"env":"prd","params":'
                    f'{{"token":"REDACTED:jwt:sha256={g[:12]}","gameID":"{game}",'
                    f'"mathVer":"feature_base","gameType":{ptype},"fbMaxAllowBet":200}},'
                    f'"resCode":{res_code},"response":{{"messageCode":{res_code},'
                    f'"gameSeqId":"{g}","betAmount":{bet},"beforeBalance":{before},'
                    f'"afterBalance":{after},"win":{win}}},"service":"spin-server",'
                    f'"ts":"{t.isoformat()}Z"}}')} for i in range(res_count)]
    start = (t - _dt.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (t + _dt.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "game_seq_id": g,
        "round_time_utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_minutes": 1,
        "filter": ('resource.type="k8s_container" AND '
                   'resource.labels.container_name="spin-server" AND '
                   f'timestamp>="{start}" AND timestamp<="{end}" AND '
                   f'textPayload:"{g}"'),
        "identity": "airc-740@acp-prod.iam.gserviceaccount.com",
        "entries": entries,
    }


def write_case(name, rows, fixtures):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"inject_{name}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader(); w.writerows(rows)
    d = LOGS / f"P_{name}"; d.mkdir(parents=True, exist_ok=True)
    for g, doc in fixtures.items():
        doc["case_id"] = f"P_{name}"
        (d / f"{g}.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False),
                                     encoding="utf-8")
    serials = [r["serial"] for r in rows]
    gt = {"scenario": name, "uid": rows[-1]["uid"] if name != "A01" else rows[0]["uid"],
          "operator": rows[0]["parent"], "game_id": rows[0]["game_id"],
          "serial_min": min(serials), "serial_max": max(serials),
          "rows": len(rows), "attack_gsids": list(fixtures)}
    return path, gt


def build_A01():
    parent, game, ccy = "jdbygmmk", "1049s", "MMK"
    uid = stealth_uid(parent)
    rows, fx, bal, s = [], {}, 10000.0, SERIAL_BASE
    for i in range(8):
        r, bal = row(s + i, gsid(), uid, parent, game, 0, ccy, 100,
                     0 if i % 3 else 60, bal, False, _ts(i * 3))
        rows.append(r)
    g = gsid(); win = 7500.0
    r1, bal = row(s + 50, g, uid, parent, game, 0, ccy, 100, win, bal, True, _ts(60))
    rows.append(r1)
    r2, bal = row(s + 51, g, uid, parent, game, 0, ccy, 100, win, bal, True, _ts(61))
    rows.append(r2)                                   # duplicate settlement, same gsid
    fx[g] = spinlog(g, game, 0, ccy, 100, win, r1["before_balance"], _ts(60),
                    res_count=2)                      # 2 responses = genuine double-settle
    return write_case("A01", rows, fx)


def build_A09():
    parent, game, ccy = "jdbygmmk", "1049s", "MMK"
    uid = stealth_uid(parent)
    cap = CERT[game][0][1]
    rows, fx, bal, s = [], {}, 8000.0, SERIAL_BASE + 100_000
    for i in range(6):
        r, bal = row(s + i, gsid(), uid, parent, game, 0, ccy, 50,
                     0 if i % 2 else 40, bal, False, _ts(i * 2))
        rows.append(r)
    g = gsid(); bet, win = 1.0, 5200.0                # 5200x > 5000x cap
    r, bal = row(s + 30, g, uid, parent, game, 0, ccy, bet, win, bal, True, _ts(40))
    rows.append(r)
    fx[g] = spinlog(g, game, 0, ccy, bet, win, r["before_balance"], _ts(40))
    return write_case("A09", rows, fx)


def build_A23():
    """Feature Buy Exploit, REPEATED by the SAME uid across three days
    (2026-08-01, 08-05, 08-07) -- persistent advantage play, not a one-day
    spike. The pt2 cell aggregates all three days (well past the 200-round
    EXCESS floor) and the repetition is what lets the split-half persistence
    check hold, instead of collapsing under the single-spike test."""
    parent, game, ccy = "mlcafecny", "1045s", "CNY"
    uid = stealth_uid(parent)              # ONE uid, all three days
    price = 32.5
    days = [_dt.date(2026, 8, 1), _dt.date(2026, 8, 5), _dt.date(2026, 8, 7)]
    rng = random.Random(4123)
    rows, fx, bal = [], {}, 50000.0
    day_ranges = {}
    for di, day in enumerate(days):
        base_serial = SERIAL_BASE + 200_000 + di * 10_000
        n_buy = 90                         # 90/day x 3 = 270 pt2 rounds in the cell
        first = base_serial
        for i in range(n_buy):
            g = gsid()
            # each day carries its own big win so RTP ~2.0 on every day
            win = price * (rng.uniform(60, 120) if i in (30, 70)
                           else rng.uniform(0.0, 2.2))
            r, bal = row(base_serial + i, g, uid, parent, game, 2, ccy, price,
                         round(win, 2), bal, True, _ts(i * 8, day))
            rows.append(r)
            if i < 2 or i in (30, 70):     # a few fixtures per day
                fx[g] = spinlog(g, game, 2, ccy, price, round(win, 2),
                                r["before_balance"], _ts(i * 8, day))
        for j in range(8):                 # a little base play each day
            r, bal = row(base_serial + 500 + j, gsid(), uid, parent, game, 0,
                         ccy, 1.0, round(rng.uniform(0, 2), 2), bal, False,
                         _ts(3000 + j * 5, day))
            rows.append(r)
        day_ranges[day.isoformat()] = [first, rows[-1]["serial"]]
    path, gt = write_case("A23", rows, fx)
    gt["repeated_days"] = day_ranges       # ground truth: same uid, 3 days
    return path, gt


def build_A32():
    """Bet Timing Exploit (prescient betting).
    Signature: the player bets SMALL on ordinary base rounds but LARGE precisely
    on the rounds that trigger the feature -- stake predicts a favourable
    outcome, which honest math forbids (outcome is independent of the stake
    chosen for the round).
    DB -> L1-PRESCIENT_BET (exact permutation null on mean(bet|triggered)).
    Needs play_type=0, >= 2000 base rounds, >= 30 trigger events, >1 bet level."""
    parent, game, ccy = "jdbygmmk", "1049s", "MMK"
    uid = stealth_uid(parent)               # ONE uid, both days
    rng = random.Random(0x32BEEF)
    days = [_dt.date(2026, 8, 2), _dt.date(2026, 8, 8)]
    per_day = 1300                          # 1300 x 2 = 2600 base rounds (>= 2000)
    small = [0.4, 0.6, 1.0]
    rows, fx, bal = [], {}, 20000.0
    day_ranges = {}
    for di, day in enumerate(days):
        base_serial = SERIAL_BASE + 300_000 + di * 10_000
        trig_at = set(rng.sample(range(40, per_day), 18))   # 18 triggers/day -> 36 total
        first = base_serial
        for i in range(per_day):
            g = gsid()
            t = _ts(i * 13, day)            # ~13s cadence: human, not a bot
            if i in trig_at:
                bet = 50.0                  # LARGE stake, only on triggers
                win = round(bet * rng.uniform(3, 40), 2)
                r, bal = row(base_serial + i, g, uid, parent, game, 0, ccy, bet,
                             win, bal, True, t)
                if len(fx) < 8:
                    fx[g] = spinlog(g, game, 0, ccy, bet, win, r["before_balance"], t)
            else:
                bet = rng.choice(small)
                win = round(bet * rng.uniform(0, 3), 2) if rng.random() < 0.30 else 0.0
                r, bal = row(base_serial + i, g, uid, parent, game, 0, ccy, bet,
                             win, bal, False, t)
            rows.append(r)
            if bal < bet + 100:             # keep wallet solvent (identity preserved)
                bal += 5000.0
                rows[-1]["before_balance"] = round(rows[-1]["before_balance"] + 5000.0, 5)
                rows[-1]["after_balance"] = round(rows[-1]["after_balance"] + 5000.0, 5)
        day_ranges[day.isoformat()] = [first, rows[-1]["serial"]]
    path, gt = write_case("A32", rows, fx)
    gt["repeated_days"] = day_ranges        # same uid, 08-02 and 08-08
    return path, gt


def build_A36():
    """'Knows when to bet big.'  The player plays small most of the time, but
    on MANY separate occasions raises the stake right before a big prize -- and
    the big-bet rounds win almost every single time. Repeated across three days.
    This is the extreme, general form of prescient betting (predicts high-value
    outcomes, not just feature triggers) with a near-100% prediction success
    rate -- the DB shadow of a predictable-RNG exploit.
    DB -> L1-PRESCIENT_BET (stake predicts the big-win rounds) + L1-EXCESS_TURNOVER.
    (The distinguishing 'prediction success ~100%' statistic has no calibrated
    rule yet -- see manifest; it fires today via its PRESCIENT/EXCESS shadow.)"""
    parent, game, ccy = "gkkmmk", "1049s", "MMK"
    uid = stealth_uid(parent)               # ONE uid, all three days
    rng = random.Random(0x36AACE)
    days = [_dt.date(2026, 8, 1), _dt.date(2026, 8, 2), _dt.date(2026, 8, 3)]
    ramp = [50.0, 75.0, 100.0, 125.0, 150.0]   # the "increase bet" ladder
    rows, fx, bal = [], {}, 30000.0
    day_ranges = {}
    for di, day in enumerate(days):
        base_serial = SERIAL_BASE + 400_000 + di * 10_000
        per_day = 700                       # small-stake background
        big_at = set(rng.sample(range(20, per_day), 16))   # 16 big-bet events/day
        first = base_serial
        for i in range(per_day):
            g = gsid()
            t = _ts(i * 20, day)            # ~20s cadence: human
            if i in big_at:
                bet = rng.choice(ramp)      # raises the stake...
                win = round(bet * rng.uniform(20, 60), 2)   # ...and wins big, essentially always
                r, bal = row(base_serial + i, g, uid, parent, game, 0, ccy, bet,
                             win, bal, True, t)
                if len(fx) < 9:
                    fx[g] = spinlog(g, game, 0, ccy, bet, win, r["before_balance"], t)
            else:
                bet = 1.0                    # ordinary small play
                win = round(bet * rng.uniform(0, 3), 2) if rng.random() < 0.30 else 0.0
                r, bal = row(base_serial + i, g, uid, parent, game, 0, ccy, bet,
                             win, bal, False, t)
            rows.append(r)
            if bal < bet + 200:             # keep wallet solvent (identity preserved)
                bal += 20000.0
                rows[-1]["before_balance"] = round(rows[-1]["before_balance"] + 20000.0, 5)
                rows[-1]["after_balance"] = round(rows[-1]["after_balance"] + 20000.0, 5)
        day_ranges[day.isoformat()] = [first, rows[-1]["serial"]]
    path, gt = write_case("A36", rows, fx)
    gt["repeated_days"] = day_ranges
    return path, gt


def manifest(gts):
    by = {g["scenario"]: g for g in gts}
    lines = [
        "# Synthetic risk injections — FOR REVIEW (not yet in BigQuery)",
        "",
        f"Target event date **{DAY}** (UTC `game_time`); business date {REPORT}.",
        "",
        "**Stealth:** attacker uids mimic each operator's real naming and gsids "
        "are ordinary uuids, so the engine gets no free hint. The real sm_tag "
        f"`{SM_TAG}` is used verbatim. **Removal key is `serial >= {SERIAL_BASE:,}`** "
        "— a channel detection never inspects. Exact uids/serials/gsids below are "
        "the cleanup ledger AND the replay ground truth.",
        "",
        "| Scenario | rows | stealth uid | operator / game | serial range | DB signature | expected rule | log fixture |",
        "|---|---:|---|---|---|---|---|---|",
        f"| **A01** Replay / Duplicate | {by['A01']['rows']} | `{by['A01']['uid']}` | jdbygmmk / 1049s | {by['A01']['serial_min']}–{by['A01']['serial_max']} | one `game_seq_id` settled twice | `L1-DUP_ROUND` | **2** responses → double-settle (vs ETL=1) |",
        f"| **A09** Max Win Bypass | {by['A09']['rows']} | `{by['A09']['uid']}` | jdbygmmk / 1049s | {by['A09']['serial_min']}–{by['A09']['serial_max']} | win/valid_bet 5200× > 5000× cap | `L1-MAX_X_BREACH` | 1 response, win 5200 on bet 1.0 |",
        f"| **A23** Feature Buy (×3 days) | {by['A23']['rows']} | `{by['A23']['uid']}` | mlcafecny / 1045s | {by['A23']['serial_min']}–{by['A23']['serial_max']} | SAME uid repeats on 08-01/08-05/08-07; 270 pt2 rounds, RTP≈2.0 vs cert 0.9667, buy-share≈99% | `L1-FEATURE_ROUTING` + `L1-EXCESS_TURNOVER` + **persistence held** | feature-buy responses across all 3 days |",
        f"| **A32** Bet Timing (prescient, ×2 days) | {by['A32']['rows']} | `{by['A32']['uid']}` | jdbygmmk / 1049s | {by['A32']['serial_min']}–{by['A32']['serial_max']} | SAME uid on 08-02 & 08-08; 2600 base rounds; bets 0.4–1.0 normally but **50 only on the 36 triggering rounds** — stake predicts the feature | `L1-PRESCIENT_BET` | trigger-round responses across both days |",
        f"| **A36** Knows-when-to-bet-big | {by['A36']['rows']} | `{by['A36']['uid']}` | gkkmmk / 1049s | {by['A36']['serial_min']}–{by['A36']['serial_max']} | SAME uid on 08-01/02/03; small play throughout but raises the stake (50–150) before a big win **48 times, winning nearly every time** | `L1-PRESCIENT_BET` + `L1-EXCESS_TURNOVER` (distinct 'prediction success ≈100%' stat has no rule yet — engine gap) | big-bet→big-win responses across all 3 days |",
        "",
        "## Review checklist",
        "- A01: the two rows share `game_seq_id`, differ in `serial`, both credit the win.",
        "- A09: exactly one breach row; win/valid_bet strictly above the OMG 1049s cap.",
        "- A23: pt2 cell ≥200 rounds (EXCESS eligibility); base rounds keep buy-share <100%.",
        "- All: `after_balance == before_balance - bet + win` on every row (no false integrity hit).",
        "",
        "## Next step — only on your go-ahead (paid ~$0.4)",
        f"1. Collision check: `SELECT uid FROM sim WHERE uid IN ({by['A01']['uid']!r},"
        f" {by['A09']['uid']!r}, {by['A23']['uid']!r})` must return 0 rows.",
        "2. `CREATE OR REPLACE TABLE OMG_riskdet_w1.rounds_all AS SELECT * FROM archive` (Aug slice).",
        "3. MERGE these CSVs; replay `rounds_asof` day-by-day; record first-detection day per rule.",
        f"4. Cleanup any time: `DELETE FROM OMG_riskdet_w1.rounds_all WHERE serial >= {SERIAL_BASE}`.",
        "",
        "Log fixtures in `out/logs_sim/` are shaped identically to real "
        "scripts/pull_logs.py evidence (no `simulated` flag) so Layer-4 review "
        "is blind. Provenance = the directory isolation + the gsid ledger in "
        "ground_truth.json; they are never written to acp-prod and out/logs_sim/ "
        "is gitignored.",
    ]
    (OUT / "manifest.md").write_text("\n".join(lines), encoding="utf-8")
    (OUT / "ground_truth.json").write_text(json.dumps(gts, indent=2), encoding="utf-8")


if __name__ == "__main__":
    gts = []
    for fn in (build_A01, build_A09, build_A23, build_A32, build_A36):
        path, gt = fn()
        gts.append(gt)
        print(f"{path.name:18} {gt['rows']:4} rows  uid={gt['uid']:14} "
              f"serial {gt['serial_min']}–{gt['serial_max']}")
    manifest(gts)
    print(f"\nmanifest     -> {OUT / 'manifest.md'}")
    print(f"ground truth -> {OUT / 'ground_truth.json'}")
    print(f"log fixtures -> {LOGS}")
