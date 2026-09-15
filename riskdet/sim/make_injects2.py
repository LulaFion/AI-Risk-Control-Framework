"""Synthetic risk injections — BATCH 2 (Game-Bug scenarios) FOR REVIEW.

Eight "player-wins" Game-Bug scenarios from Risk Scenario II, all within
2026-08-04 .. 2026-08-10. Local files only -- NOTHING is written to BigQuery
here (review first, then a separate load step, exactly like batch 1).

Per your spec: each scenario is exhibited by 1-3 stealth accounts (IDs), and
each account is active on 1-5 days inside the window (deterministic seed).

All eight OVERPAY the player (operator financial exposure):
  D04 Missing Wager Deduction     win credited, bet not deducted     -> BALANCE_IDENTITY
  D06 Split Settlement Overflow   wallet credited above the payout   -> BALANCE_IDENTITY
  D07 Rollback Reconciliation     wager refunded but not reconciled  -> BALANCE_IDENTITY
  D12 Max-Win Early Termination   wins keep paying past the cap       -> MAX_X_BREACH + EXCESS
  D15 Invalid Feature Entry       feature entered w/o trigger cond.   -> TRIGGER_RATE + EXCESS
  D16 Feature Re-entry Bug        feature re-entered repeatedly       -> TRIGGER_RATE + EXCESS
  D54 RTP Shift After Deployment  base RTP jumps (~140%)              -> EXCESS_TURNOVER
  D55 Feature RTP Spike           feature RTP jumps (~250%)           -> EXCESS_TURNOVER

CAVEAT (D54/D55): a true game-level RTP shift needs a broad cohort to move the
game aggregate; with 1-3 IDs these surface at PLAYER grain (the accounts' own
elevated RTP), not as a game-cell aggregate shift. Flagged in ground truth.

Stealth identical to batch 1: uids mimic operator naming, real sm_tag verbatim,
gsids are ordinary uuids. Removal key: serial >= 9_200_000_000 (batch-2 block,
still inside the >= 9_100_000_000 synthetic space). Balance identity is broken
ONLY where a scenario requires it (D04/D06/D07); everywhere else it holds.
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
SM_TAG = "0.2.2.20260102"
SERIAL_BASE2 = 9_200_000_000
WINDOW = [_dt.date(2026, 8, d) for d in range(4, 11)]   # 08-04 .. 08-10 (7 days)


def _stealth_uid_raw(rng: random.Random, operator: str) -> str:
    if operator == "jdbygmmk":
        return "yg" + "".join(rng.choice("0123456789abcdefghjkmnpqrstuvwxyz") for _ in range(8))
    if operator == "mlcafecny":
        return f"1041ml{rng.randrange(700, 990)}"
    if operator == "gkkmmk":
        return "gk" + "".join(rng.choice("0123456789") for _ in range(9))
    return operator[:2] + "".join(rng.choice("0123456789") for _ in range(9))


# global uniqueness -- a uid must be unique across ALL batch-2 accounts AND must
# not reuse a batch-1 uid (else two distinct "accounts" merge into one cell and
# corrupt the ground truth / cross-contaminate scenarios).
_SEEN_UIDS: set[str] = set()


def _preload_batch1_uids() -> None:
    p = OUT / "ground_truth.json"
    if p.is_file():
        for g in json.loads(p.read_text()):
            if g.get("uid"):
                _SEEN_UIDS.add(g["uid"])


def stealth_uid(rng: random.Random, operator: str) -> str:
    for _ in range(10000):
        u = _stealth_uid_raw(rng, operator)
        if u not in _SEEN_UIDS:
            _SEEN_UIDS.add(u)
            return u
    raise RuntimeError(f"uid space exhausted for {operator}")


def gsid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _ts(day: _dt.date, sec: int) -> _dt.datetime:
    return _dt.datetime(day.year, day.month, day.day, 12, 0, 0) + _dt.timedelta(seconds=sec)


def row(serial, g, uid, parent, game, ptype, ccy, bet, win, before, triggered, t,
        after_override=None, sm_tag=SM_TAG):
    """Identity-holding row unless after_override is given (integrity break)."""
    after = round(before - bet + win if after_override is None else after_override, 5)
    return {
        "game_time": t.strftime("%Y-%m-%dT%H:%M:%S"), "report_date": t.date().isoformat(),
        "serial": serial, "game_seq_id": g, "parent": parent, "uid": uid,
        "game_id": game, "play_type": ptype, "currency": ccy,
        "sm_v": "feature_base", "sm_tag": sm_tag, "bet": round(bet, 5),
        "valid_bet": round(bet, 5), "win": round(win, 5),
        "before_balance": round(before, 5), "after_balance": after,
        "triggered": triggered,
        "last_modify_time": (t + _dt.timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S"),
    }, after


def spinlog(g, game, ptype, ccy, bet, win, before, after, t, res_count=1, res_code=1000):
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
    return {"game_seq_id": g, "round_time_utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window_minutes": 1,
            "filter": ('resource.type="k8s_container" AND '
                       'resource.labels.container_name="spin-server" AND '
                       f'timestamp>="{start}" AND timestamp<="{end}" AND textPayload:"{g}"'),
            "identity": "airc-740@acp-prod.iam.gserviceaccount.com", "entries": entries}


def pick_ids_days(seed: int, n_ids: int | None = None) -> list[list[_dt.date]]:
    """IDs x days. Default 1-3 IDs (each active 1-5 days). A scenario may force a
    larger cohort via n_ids -- e.g. a deployment RTP shift affects many accounts
    on the build at once, so it must be a cohort to read as game-level, not
    individual advantage play."""
    r = random.Random(seed)
    k = n_ids if n_ids is not None else r.randint(1, 3)
    return [sorted(r.sample(WINDOW, r.randint(1, 5))) for _ in range(k)]


# --------------------------------------------------------------------------- #
# One builder signature: returns (rows, fixtures, per_id_ground_truth)
# --------------------------------------------------------------------------- #
def _base_block(scn_idx, id_idx, day_idx):
    return SERIAL_BASE2 + scn_idx * 1_000_000 + id_idx * 100_000 + day_idx * 10_000


def build(scn, scn_idx, operator, game, ccy, seed, gen_day, ptype=0,
          fixtures_per_id=4, n_ids=None):
    """Generic driver: gen_day(rng, ctx) yields rows for one (id, day)."""
    ids_days = pick_ids_days(seed, n_ids)
    rng = random.Random(seed ^ 0x5EED)
    rows, fx, gts = [], {}, []
    for id_idx, days in enumerate(ids_days):
        uid = stealth_uid(rng, operator)
        bal = 50000.0
        id_serials, nfx = [], 0
        for day_idx, day in enumerate(days):
            s0 = _base_block(scn_idx, id_idx, day_idx)
            ctx = {"uid": uid, "parent": operator, "game": game, "ccy": ccy,
                   "ptype": ptype, "s0": s0, "day": day, "bal": bal}
            produced = gen_day(rng, ctx)
            for r, logfields in produced:
                rows.append(r)
                id_serials.append(r["serial"])
                bal = r["after_balance"]
                if logfields and nfx < fixtures_per_id:
                    g = r["game_seq_id"]
                    fx[g] = spinlog(g, game, r["play_type"], ccy, r["bet"], r["win"],
                                    r["before_balance"], r["after_balance"],
                                    _dt.datetime.strptime(r["game_time"], "%Y-%m-%dT%H:%M:%S"),
                                    **logfields)
                    nfx += 1
        gts.append({"scenario": scn, "uid": uid, "operator": operator, "game_id": game,
                    "play_type": ptype, "currency": ccy,
                    "serial_min": min(id_serials), "serial_max": max(id_serials),
                    "rows": len(id_serials),
                    "days": [d.isoformat() for d in days]})
    return rows, fx, gts


# ---- per-scenario day generators ----------------------------------------- #
def gen_D04(rng, c):  # missing wager deduction: win credited, bet not taken
    out = []
    n = rng.randint(40, 70)
    for i in range(n):
        g = gsid(rng); bet = rng.choice([1.0, 2.0, 5.0])
        broke = rng.random() < 0.35
        win = round(bet * rng.uniform(1.5, 8), 2) if broke else (
            round(bet * rng.uniform(0, 2), 2) if rng.random() < 0.3 else 0.0)
        b = c["bal"] if not out else out[-1][0]["after_balance"]
        over = (b + win) if broke else None           # after = before + win (bet not deducted)
        r, _ = row(c["s0"] + i, g, c["uid"], c["parent"], c["game"], 0, c["ccy"],
                   bet, win, b, broke, _ts(c["day"], i * 30), after_override=over)
        out.append((r, {"res_count": 1} if broke else None))
    return out


def gen_D06(rng, c):  # split settlement overflow: over-credit beyond the win
    out = []
    n = rng.randint(40, 70)
    for i in range(n):
        g = gsid(rng); bet = rng.choice([2.0, 5.0, 10.0])
        win_round = rng.random() < 0.5
        win = round(bet * rng.uniform(2, 12), 2) if win_round else 0.0
        b = c["bal"] if not out else out[-1][0]["after_balance"]
        broke = win_round and rng.random() < 0.5
        over = (b - bet + win + round(win * rng.uniform(0.4, 1.0), 2)) if broke else None
        r, _ = row(c["s0"] + i, g, c["uid"], c["parent"], c["game"], 0, c["ccy"],
                   bet, win, b, win_round, _ts(c["day"], i * 25), after_override=over)
        out.append((r, {"res_count": 2} if broke else None))
    return out


def gen_D07(rng, c):  # rollback reconciliation: wager refunded, not reconciled
    out = []
    n = rng.randint(40, 70)
    for i in range(n):
        g = gsid(rng); bet = rng.choice([1.0, 2.0, 5.0])
        win = round(bet * rng.uniform(0, 2), 2) if rng.random() < 0.3 else 0.0
        b = c["bal"] if not out else out[-1][0]["after_balance"]
        broke = rng.random() < 0.4                    # wager refunded post-settle
        over = (b + win) if broke else None           # bet returned (not subtracted)
        r, _ = row(c["s0"] + i, g, c["uid"], c["parent"], c["game"], 0, c["ccy"],
                   bet, win, b, False, _ts(c["day"], i * 28), after_override=over)
        out.append((r, {"res_count": 1} if broke else None))
    return out


def gen_D12(rng, c):  # max-win early termination: wins keep paying past the cap
    out = []
    cap = CERT[c["game"]][0][1]
    n = rng.randint(80, 140)
    burst = set(rng.sample(range(20, n), rng.randint(6, 12)))   # over-cap win cluster
    for i in range(n):
        g = gsid(rng); bet = rng.choice([1.0, 2.0])
        b = c["bal"] if not out else out[-1][0]["after_balance"]
        if i in burst:
            win = round(bet * cap * rng.uniform(1.02, 1.25), 2)   # > cap -> breach
            r, _ = row(c["s0"] + i, g, c["uid"], c["parent"], c["game"], 0, c["ccy"],
                       bet, win, b, True, _ts(c["day"], i * 15))
            out.append((r, {"res_count": 1}))
        else:
            win = round(bet * rng.uniform(0, 3), 2) if rng.random() < 0.3 else 0.0
            r, _ = row(c["s0"] + i, g, c["uid"], c["parent"], c["game"], 0, c["ccy"],
                       bet, win, b, False, _ts(c["day"], i * 15))
            out.append((r, None))
    return out


def gen_feature(trigger_rate, win_lo, win_hi):        # shared for D15 / D16
    def _g(rng, c):
        out = []
        n = rng.randint(90, 160)
        clustered = trigger_rate > 0.06                # D16 re-entry = clusters
        trig = set()
        if clustered:
            i = rng.randint(5, 15)
            while i < n:
                for k in range(rng.randint(2, 4)):
                    if i + k < n:
                        trig.add(i + k)
                i += rng.randint(8, 18)
        else:
            trig = set(rng.sample(range(n), max(1, int(n * trigger_rate))))
        for i in range(n):
            g = gsid(rng); bet = rng.choice([1.0, 2.0, 5.0])
            b = c["bal"] if not out else out[-1][0]["after_balance"]
            if i in trig:
                win = round(bet * rng.uniform(win_lo, win_hi), 2)
                r, _ = row(c["s0"] + i, g, c["uid"], c["parent"], c["game"], 0, c["ccy"],
                           bet, win, b, True, _ts(c["day"], i * 12))
                out.append((r, {"res_count": 1}))
            else:
                win = round(bet * rng.uniform(0, 2), 2) if rng.random() < 0.25 else 0.0
                r, _ = row(c["s0"] + i, g, c["uid"], c["parent"], c["game"], 0, c["ccy"],
                           bet, win, b, False, _ts(c["day"], i * 12))
                out.append((r, None))
        return out
    return _g


def gen_rtp(mult_lo, mult_hi, ptype, price=None, sm_tag=SM_TAG):  # shared D54 / D55
    def _g(rng, c):
        out = []
        n = rng.randint(80, 140)
        for i in range(n):
            g = gsid(rng)
            bet = price if price else rng.choice([1.0, 2.0, 5.0])
            b = c["bal"] if not out else out[-1][0]["after_balance"]
            # elevated RTP: most rounds pay above the certified return
            win = round(bet * rng.uniform(mult_lo, mult_hi), 2)
            trig = ptype != 0
            r, _ = row(c["s0"] + i, g, c["uid"], c["parent"], c["game"], ptype, c["ccy"],
                       bet, win, b, trig, _ts(c["day"], i * 10), sm_tag=sm_tag)
            out.append((r, {"res_count": 1} if i < 3 else None))
        return out
    return _g


def write_all(all_rows, all_fx, all_gts):
    OUT.mkdir(parents=True, exist_ok=True)
    # one CSV per scenario
    by_scn = {}
    for r in all_rows:
        by_scn.setdefault(r["_scn"], []).append({k: r[k] for k in COLS})
    for scn, rows in by_scn.items():
        with (OUT / f"inject_{scn}.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=COLS)
            w.writeheader(); w.writerows(rows)
    # log fixtures grouped by scenario
    for scn, fxmap in all_fx.items():
        d = LOGS / f"P_{scn}"; d.mkdir(parents=True, exist_ok=True)
        for g, doc in fxmap.items():
            doc["case_id"] = f"P_{scn}"
            (d / f"{g}.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False),
                                         encoding="utf-8")
    (OUT / "ground_truth2.json").write_text(json.dumps(all_gts, indent=2), encoding="utf-8")


# tuple: (scn, idx, operator, game, ccy, seed, gen, ptype, desc, rule, n_ids)
# n_ids=None -> 1-3 IDs per spec; a number -> fixed cohort (deployment-wide shift)
SPECS = [
    ("D04", 0, "jdbygmmk", "1049s", "MMK", 0xD04, gen_D04, 0,
     "Missing Wager Deduction — win credited, bet not deducted", "L1-BALANCE_IDENTITY", None),
    ("D06", 1, "gkkmmk", "1049s", "MMK", 0xD06, gen_D06, 0,
     "Split Settlement Overflow — wallet credited above the payout", "L1-BALANCE_IDENTITY", None),
    ("D07", 2, "mlcafecny", "1045s", "CNY", 0xD07, gen_D07, 0,
     "Rollback Reconciliation — wager refunded, not reconciled", "L1-BALANCE_IDENTITY", None),
    ("D12", 3, "jdbygmmk", "1049s", "MMK", 0xD12, gen_D12, 0,
     "Max-Win Early Termination — wins keep paying past the cap",
     "L1-MAX_X_BREACH + L1-EXCESS_TURNOVER", None),
    ("D15", 4, "gkkmmk", "1049s", "MMK", 0xD15, gen_feature(0.05, 12, 45), 0,
     "Invalid Feature Entry — feature entered without trigger conditions",
     "L1-TRIGGER_RATE + L1-EXCESS_TURNOVER", None),
    ("D16", 5, "mlcafecny", "1045s", "CNY", 0xD16, gen_feature(0.09, 6, 30), 0,
     "Feature Re-entry — feature re-entered repeatedly (clusters)",
     "L1-TRIGGER_RATE + L1-EXCESS_TURNOVER", None),
    ("D54", 6, "pph855usd", "1052s", "USD", 0xD54,
     gen_rtp(1.2, 1.6, 0, sm_tag="0.0.14.20260303"), 0,
     "RTP Shift After Deployment — base RTP ~140% on LOW-VOLUME cell "
     "1052s/pt0/USD (real cell ~285 rounds/wk; 25-account cohort dominates it "
     "-> game-cell aggregate drift)",
     "L2-OFF_TARGET_RTP (game-cell aggregate RTP vs certified)", 25),
    ("D55", 7, "mlcafecny", "1045s", "CNY", 0xD55, gen_rtp(2.0, 3.2, 2, price=32.5), 2,
     "Feature RTP Spike — feature (pt2) RTP ~250%, feature-wide cohort",
     "L1-EXCESS_TURNOVER across many accounts on the feature (spike signature)", 15),
]


if __name__ == "__main__":
    _preload_batch1_uids()
    all_rows, all_fx, all_gts = [], {}, []
    print(f"window {WINDOW[0]} .. {WINDOW[-1]}   removal key serial >= {SERIAL_BASE2:,}\n")
    for (scn, idx, op, game, ccy, seed, gen, pt, desc, rule, n_ids) in SPECS:
        rows, fx, gts = build(scn, idx, op, game, ccy, seed, gen, ptype=pt, n_ids=n_ids)
        for r in rows:
            r["_scn"] = scn
        all_rows.extend(rows)
        all_fx[scn] = fx
        for gt in gts:
            gt["footprint"] = desc
            gt["expected_rule"] = rule
        all_gts.extend(gts)
        ids = ", ".join(f"{g['uid']}({len(g['days'])}d,{g['rows']}r)" for g in gts)
        print(f"{scn}: {len(gts)} id(s) -> {ids}")
    write_all(all_rows, all_fx, all_gts)
    print(f"\ntotal rows: {len(all_rows)} across {len(SPECS)} scenarios")
    print(f"ground truth -> {OUT / 'ground_truth2.json'}")
