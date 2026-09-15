"""Detector tests for the per-minute Cloud Logging monitor.

A clean production run proves only that these do not FALSE-POSITIVE. These tests
prove the opposite half -- that each one actually fires on the pattern it claims
to detect -- using synthetic records, so they need no credentials and no network.

The double-settle case is the one that matters most: the first live run reported
1,657 double-settles because it counted the internal `[Core-Play] betNsettle`
log line as a second settlement. That is a ~9% false-positive rate on ordinary
traffic, and the guard against its return is `test_double_settle_ignores_*`.
"""
import datetime as _dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from riskdet.cloudlogs import monitor as M  # noqa: E402

T0 = _dt.datetime(2026, 9, 14, 9, 0, 0)


def _rec(gsid, code, *, offset_s=0.0, path=None, agent=None, message="",
         player="tok_a"):
    return {"ts": T0 + _dt.timedelta(seconds=offset_s), "gsid": gsid,
            "res_code": code, "path": path, "method": "POST" if path else None,
            "agent": agent, "game_id": "1049s", "sm_tag": "0.2.4",
            "currency": "MMK", "player_key": player, "message": message,
            "pod": "spin-server-1"}


# --------------------------------------------------------------------- #
# 1. double-settle
# --------------------------------------------------------------------- #
def test_double_settle_fires_on_two_settlements():
    hits = M._detect_double_settle([
        _rec("g1", 1000), _rec("g1", 1000, offset_s=0.3), _rec("g2", 1000)])
    assert len(hits) == 1
    assert hits[0]["gsid"] == "g1" and hits[0]["responses"] == 2


def test_double_settle_silent_on_normal_traffic():
    assert M._detect_double_settle([_rec("g1", 1000), _rec("g2", 1000)]) == []


def test_double_settle_ignores_failed_attempts():
    """A failure plus a success is a REPLAY, not a double-settle -- the money
    only moved once."""
    assert M._detect_double_settle([_rec("g1", 802), _rec("g1", 1000)]) == []


# --------------------------------------------------------------------- #
# 2. retry-until-favourable (round replay)
# --------------------------------------------------------------------- #
def test_replay_fires_on_failure_then_success():
    hits = M._detect_round_replay([
        _rec("g1", 802, message="Your Cash Balance not enough."),
        _rec("g1", 1000, offset_s=2.0)])
    assert len(hits) == 1
    assert hits[0]["gsid"] == "g1" and 802 in hits[0]["failed_codes"]


def test_replay_silent_on_a_lone_failure():
    """802 = 'Cash Balance not enough' is ordinary. Only failure->success on the
    SAME round id is evidence that an attempt was repeated until it paid."""
    assert M._detect_round_replay([_rec("g1", 802)]) == []


def test_replay_silent_on_plain_success():
    assert M._detect_round_replay([_rec("g1", 1000)]) == []


def test_replay_ignores_request_log_lines():
    """resCode 0 marks an inbound request log, not an outcome."""
    assert M._detect_round_replay([_rec("g1", 0), _rec("g1", 1000)]) == []


# --------------------------------------------------------------------- #
# 3. API path bypass
# --------------------------------------------------------------------- #
def test_bypass_fires_on_unexpected_path():
    hits = M._detect_path_bypass([_rec("g1", 0, path="/internal/admin/settle")])
    assert len(hits) == 1 and hits[0]["kind"] == "path"


def test_bypass_silent_on_the_normal_spin_path():
    assert M._detect_path_bypass([_rec("g1", 0, path="/v2/1049s/spin")]) == []


def test_bypass_fires_on_unregistered_agent():
    hits = M._detect_path_bypass([
        _rec("g1", 901, agent="stgojp01",
             message="Agent not found: stgojp01")])
    assert len(hits) == 1 and hits[0]["kind"] == "unknown_agent"


# --------------------------------------------------------------------- #
# 4. millisecond cadence census (observation only -- must not fire)
# --------------------------------------------------------------------- #
def test_cadence_census_measures_sub_second_gaps():
    recs = [_rec("g1", 1000, offset_s=0.00),
            _rec("g2", 1000, offset_s=0.02),    # 20 ms
            _rec("g3", 1000, offset_s=0.50)]    # 480 ms
    c = M._cadence_census(recs)
    assert c["players_with_2plus_rounds"] == 1
    assert c["gaps_measured"] == 2
    assert c["gap_buckets"]["<50ms"] == 1
    assert c["gap_buckets"]["200ms-1s"] == 1


def test_cadence_census_separates_players():
    recs = [_rec("g1", 1000, offset_s=0.0, player="a"),
            _rec("g2", 1000, offset_s=0.01, player="b")]
    # one round each -> no intra-player gap exists
    assert M._cadence_census(recs)["gaps_measured"] == 0


def test_cadence_emits_no_signal():
    """Statistical by nature: a fixed millisecond threshold is exactly what got
    BOT_CADENCE retired. v1 accumulates for calibration and never fires."""
    sigs = M._signals([], [], [], {})
    assert sigs == []


# --------------------------------------------------------------------- #
# signal construction
# --------------------------------------------------------------------- #
def test_signals_are_absolute_and_integrity_routed():
    dbl = [{"gsid": "g1", "responses": 2, "rows": [_rec("g1", 1000)]}]
    rep = [{"gsid": "g2", "codes": [802, 1000], "failed_codes": [802],
            "rows": [_rec("g2", 802)]}]
    byp = [{"kind": "path", "detail": "/x", "row": _rec("g3", 0, path="/x")}]
    sigs = M._signals(dbl, rep, byp, {"g1": {"parent": "op", "uid": "u1",
                                             "game_id": "1049s"}})
    by_id = {s.signal_id: s for s in sigs}

    assert by_id["LOG-DOUBLE_SETTLE"].family == "INTEGRITY"
    assert by_id["LOG-DOUBLE_SETTLE"].threshold_method == "ABSOLUTE"
    assert by_id["LOG-DOUBLE_SETTLE"].uid == "u1"          # attributed
    assert by_id["LOG-ROUND_REPLAY"].family == "INTEGRITY"
    assert by_id["LOG-ROUND_REPLAY"].uid is None           # unattributed
    # a bypass is a property of the REQUEST, never of a player
    assert by_id["LOG-PATH_BYPASS"].family == "ENVIRONMENT"
    assert by_id["LOG-PATH_BYPASS"].grain == "game_cell"


def test_no_raw_jwt_can_reach_a_signal():
    """Tokens are digested at parse time; nothing downstream may carry `eyJ`."""
    dbl = [{"gsid": "g1", "responses": 2,
            "rows": [_rec("g1", 1000, player="REDACTED:jwt:sha256=abc123")]}]
    blob = repr(M._signals(dbl, [], [], {}))
    assert "eyJ" not in blob
