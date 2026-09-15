"""Integrity single-family escalation: a lone absolute breach must reach
human_review (and thus auto-enter Layer 4), while a lone statistical single
family must NOT (it stays monitor unless the exceptional route's gates pass)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import riskdet  # noqa: E402
from riskdet.merge.fuse import fuse  # noqa: E402
from riskdet.signals import Signal  # noqa: E402


def _sig(signal_id, family, *, method, baseline, value=1.0, threshold=0.0,
         p_value=None, effect=None, effect_unit="", persistence="not_computable"):
    return Signal(
        signal_id=signal_id, family=family, layer=1, grain="player_cell",
        parent="opX", uid="uidX", game_id="1049s", play_type=0,
        currency="MMK", sm_tag="0.0.1", metric="m", value=value,
        threshold=threshold, threshold_method=method, baseline_source=baseline,
        p_value=p_value, effect=effect, effect_unit=effect_unit,
        n=50, persistence=persistence)


def _cfg():
    return riskdet.settings()


def test_lone_max_x_breach_reaches_human_review():
    cands = fuse(_cfg(), [_sig("L1-MAX_X_BREACH", "INTEGRITY",
                               method="ABSOLUTE", baseline="certified",
                               value=5200.0, threshold=5000.0)])
    assert len(cands) == 1
    assert cands[0].escalation == "human_review"
    assert cands[0].severity == "Critical"          # cap breach is the top severity
    assert any("integrity single-family" in n for n in cands[0].notes)


def test_lone_balance_identity_reaches_human_review():
    cands = fuse(_cfg(), [_sig("L1-BALANCE_IDENTITY", "INTEGRITY",
                               method="ABSOLUTE", baseline="identity",
                               value=3.0)])
    assert cands[0].escalation == "human_review"
    assert cands[0].severity == "High"              # non-cap integrity


def test_lone_dup_round_reaches_human_review():
    cands = fuse(_cfg(), [_sig("L1-DUP_ROUND", "INTEGRITY",
                               method="ABSOLUTE", baseline="identity", value=2.0)])
    assert cands[0].escalation == "human_review"


def test_lone_weak_statistical_single_family_stays_monitor():
    """A single OUTCOME_MAGNITUDE z that fails the exceptional gates must NOT
    escalate -- this is the 'lucky player' the integrity route must not sweep in."""
    cands = fuse(_cfg(), [_sig("L1-EXCESS_TURNOVER", "OUTCOME_MAGNITUDE",
                               method="BINOMIAL+BH(frozen q)", baseline="certified",
                               value=3.0, threshold=None, p_value=0.01,
                               effect=5.0, effect_unit="rtp_points",
                               persistence="not_evaluated")])
    assert cands[0].escalation == "monitor"


def _drift_sig(effect, currency="USD"):
    return Signal(
        signal_id="L2-OFF_TARGET_RTP", family="OUTCOME_MAGNITUDE", layer=2,
        grain="game_cell", parent=None, uid=None, game_id="1052s", play_type=0,
        currency=currency, sm_tag="0.0.14.20260303", metric="day_excess_rtp_points",
        value=effect, threshold=None, threshold_method="ABSOLUTE+BH",
        baseline_source="certified", p_value=None, effect=effect,
        effect_unit="rtp_points", n=5000, persistence="not_evaluated")


def test_drift_persistent_reaches_human_review():
    # 2 cell-days, each +43 RTP pts vs certified -> game/platform defect
    cands = fuse(_cfg(), [_drift_sig(43.0), _drift_sig(41.0)])
    assert len(cands) == 1
    assert cands[0].escalation == "human_review"
    assert any("L2 drift route" in n for n in cands[0].notes)


def test_drift_single_day_stays_monitor():
    # only 1 cell-day -> not persistent -> stays monitor
    cands = fuse(_cfg(), [_drift_sig(43.0)])
    assert cands[0].escalation == "monitor"


def test_drift_below_effect_floor_stays_monitor():
    # 2 days but each < 2 RTP points -> below floor -> monitor
    cands = fuse(_cfg(), [_drift_sig(1.0), _drift_sig(1.5)])
    assert cands[0].escalation == "monitor"


def _excess_sig(z, effect, n, persistence):
    return Signal(
        signal_id="L1-EXCESS_TURNOVER", family="OUTCOME_MAGNITUDE", layer=1,
        grain="player_cell", parent="opX", uid="uidX", game_id="1049s", play_type=0,
        currency="MMK", sm_tag="0.0.1", metric="excess_turnover_z", value=z,
        threshold=None, threshold_method="BINOMIAL+BH(frozen q)",
        baseline_source="certified", p_value=1e-9, effect=effect,
        effect_unit="rtp_points", n=n, persistence=persistence)


def test_high_z_without_persistence_stays_monitor():
    # z=12 excess-turnover but persistence not proven -> stays monitor. z alone
    # can't separate a real exploit from a lucky account (real accounts reach
    # z=24 in w1), so escalation requires proven persistence, not a z bypass.
    cands = fuse(_cfg(), [_excess_sig(12.13, 1094.9, 1300, "not_evaluated")])
    assert cands[0].escalation == "monitor"


def test_high_z_with_persistence_held_escalates():
    # same signal, but persistence HELD -> escalates via the exceptional route
    cands = fuse(_cfg(), [_excess_sig(12.13, 1094.9, 1300, "held")])
    assert cands[0].escalation == "human_review"


def test_integrity_route_can_be_disabled(monkeypatch):
    cfg = _cfg()
    cfg.thresholds["fusion"]["integrity_single_family"] = {"enabled": False}
    cands = fuse(cfg, [_sig("L1-MAX_X_BREACH", "INTEGRITY",
                            method="ABSOLUTE", baseline="certified",
                            value=5200.0, threshold=5000.0)])
    assert cands[0].escalation == "monitor"          # route off -> single family
