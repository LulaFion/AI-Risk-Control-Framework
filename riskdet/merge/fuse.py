"""Fusion -- merge Layer 1/2/3 signals into candidates, honouring independence.

CLAUDE.md: "Escalation requires multiple independent signals or exceptionally
strong, validated evidence. Correlated signals derived from the same underlying
event must not be counted as independent confirmation."

Independence is counted by FAMILY, never by rule:
  - all TIMING rules are functions of one gap series -> at most 1;
  - RTP = hit rate x conditional win size, so OUTCOME_MAGNITUDE alongside
    OUTCOME_FREQUENCY on the SAME cell is the same money seen twice -- both are
    kept as evidence, both counted, but the decomposition is reported;
  - ENVIRONMENT (build attributes) never contributes to a player's count;
  - ML_DISCOVERY is emitted only for players no rule flagged, so it never
    co-counts with the rules it was trained on.

Escalation ladder (config `fusion:`):
  monitor            1 independent family
  enhanced           2 families, or 1 with persistence held in both halves
  human_review       >=2 families with >=1 non-ENVIRONMENT, all gates below
  exceptional route  single family, ALL of: leave-cohort-out sigma, effect
                     floor, volume floor, both-halves persistence, certified
                     baseline (config `exceptional_single_family`)

Anti-selection-bias gates, applied before human_review:
  - cohort context: members of a detected (unapproved) cohort are annotated,
    never suppressed; their case carries the dual-baseline requirement and the
    cohort's competing explanations for the skeptic;
  - expected-max-z: every candidate reports E[max z] ~ Phi^-1(1 - 1/N) for the
    population size actually tested, so "z=4.2 out of 1M players" reads as the
    noise it is.

Nothing here decides anything: output is a ranked investigation queue.
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field

import numpy as np

from ..config import Settings
from ..signals import NON_CONTRIBUTING_FAMILIES, Signal

log = logging.getLogger("riskdet.fuse")


@dataclass
class Candidate:
    case_id: str
    grain: str                         # player | game_cell
    parent: str | None
    uid: str | None
    game_id: str | None
    play_type: int | None
    currency: str | None
    sm_tag: str | None
    signals: list[Signal] = field(default_factory=list)
    families: set[str] = field(default_factory=set)
    independent_families: int = 0
    escalation: str = "monitor"        # monitor | enhanced | human_review
    severity: str = "Low"              # Critical | High | Medium | Low
    cohort_id: str | None = None
    expected_max_z: float | None = None
    notes: list[str] = field(default_factory=list)
    last_active_utc: str | None = None   # when this player last actually played
                                         # (max last_round_utc); set by the scan
    latest_evidence_utc: str | None = None   # latest SUSPICIOUS evidence-round
                                             # timestamp; the daily filter + event
                                             # dating key (set by the scan)

    @property
    def evidence_keys(self) -> list[str]:
        seen, out = set(), []
        for s in self.signals:
            for e in s.evidence:
                if e.key not in seen:
                    seen.add(e.key)
                    out.append(e.key)
        return out


def expected_max_z(n: int) -> float:
    """E[max z] over n independent null tests ~ Phi^-1(1 - 1/n)."""
    from scipy.stats import norm  # noqa: PLC0415
    return float(norm.ppf(1 - 1 / max(n, 2)))


def _exceptional_route(c: Candidate, cfg: Settings) -> bool:
    """Single-family escalation, defined quantitatively because an undefined
    'exceptionally strong evidence' clause will be used to escalate anything.
    ALL conditions must hold."""
    x = cfg.thresholds.get("fusion", {}).get("exceptional_single_family", {})
    if not x:
        return False
    zmin = float(x.get("min_sigma_leave_cohort_out", 6.0))
    eff_rtp = float(x.get("min_effect_rtp_points", 3.0))
    lift_min = float(x.get("min_rate_lift", 2.0))
    need_persist = bool(x.get("require_persistence_both_halves", True))
    need_baseline = str(x.get("require_baseline_source", "certified"))
    # NOTE: persistence is REQUIRED (not a technicality). A single excess-turnover
    # z cannot separate a real exploit from a lucky account -- in w1, 11 real
    # accounts have z>=12 (up to 24.7), overlapping the A32 inject (z=12.13). Only
    # sustained-ness (both-halves persistence) distinguishes them, so we do not
    # escalate on z alone when persistence is unproven. See detection_performance_zh.md.
    for s in c.signals:
        if s.family in NON_CONTRIBUTING_FAMILIES:
            continue
        z_ok = s.value >= zmin if s.p_value is not None else False
        eff_ok = ((s.effect_unit == "rtp_points" and (s.effect or 0) >= eff_rtp)
                  or (s.effect_unit == "lift" and (s.effect or 0) >= lift_min))
        persist_ok = (not need_persist) or s.persistence == "held"
        base_ok = (need_baseline in s.baseline_source
                   or s.baseline_source == "identity")
        if z_ok and eff_ok and persist_ok and base_ok:
            c.notes.append(
                f"exceptional single-family route: {s.signal_id} z={s.value:.1f}"
                f" effect={s.effect:.2f}{s.effect_unit} persistence="
                f"{s.persistence} baseline={s.baseline_source}")
            return True
    return False


def _integrity_route(c: Candidate, cfg: Settings) -> bool:
    """Absolute-integrity single-family escalation.

    An absolute INTEGRITY breach (paid above the certified cap; wallet identity
    `after != before - bet + win`; a duplicate-settled round) is a deterministic
    identity/contract violation, NOT a statistical tail -- it carries no
    variance-driven false-positive risk, so a SINGLE one is grounds to warn a
    human on its own. This complements `_exceptional_route`, which handles strong
    single-family *statistical* signals (those need z>=6 + persistence); integrity
    signals have no z (p_value is None) and so could never use that route.

    DUP_ROUND still needs Cloud Logging to distinguish a real double-settle from
    an ETL duplicate -- escalating it means "a human/agent must adjudicate", which
    is exactly the point; the signal already carries requires_log_adjudication.
    """
    x = cfg.thresholds.get("fusion", {}).get("integrity_single_family", {})
    if not x or not x.get("enabled", False):
        return False
    ids = set(x.get("signal_ids",
                    ["L1-MAX_X_BREACH", "L1-BALANCE_IDENTITY", "L1-DUP_ROUND"]))
    for s in c.signals:
        if (s.family == "INTEGRITY" and s.signal_id in ids
                and s.threshold_method == "ABSOLUTE"
                and s.baseline_source in ("identity", "certified")):
            c.notes.append(
                f"integrity single-family route: {s.signal_id} "
                f"({s.metric}={s.value:g} vs {s.threshold}) -- absolute breach, "
                f"deterministic (no variance false-positive risk)")
            return True
    return False


def _drift_route(c: Candidate, cfg: Settings) -> bool:
    """Game-level RTP-drift single-family escalation.

    A whole game-cell drifting from its CERTIFIED RTP is a game/platform-level
    defect (e.g. a wrong math build), not a player issue -- but it is a single
    family (OUTCOME_MAGNITUDE) and has no per-round z, so it can use neither the
    statistical exceptional route nor the integrity route. This route escalates
    it to human_review when the drift is material AND persistent AND validated:

      - >= min_effect_rtp_points against the CERTIFIED baseline (default 2.0),
      - persisted on >= min_cell_days distinct cell-days (default 2)
        (each qualifying day is one BH-FDR-selected L2-OFF_TARGET_RTP signal),
      - BH-FDR is implicit: Layer 2 only emits BH-selected days.

    Small or one-off drifts stay at monitor, so an approved deploy / promo does
    not page a human; a sustained certified-baseline drift does.
    """
    x = cfg.thresholds.get("fusion", {}).get("drift_single_family", {})
    if not x or not x.get("enabled", False):
        return False
    min_eff = float(x.get("min_effect_rtp_points", 2.0))
    min_days = int(x.get("min_cell_days", 2))
    days = [s for s in c.signals
            if s.signal_id == "L2-OFF_TARGET_RTP"
            and "certified" in (s.baseline_source or "")
            and (s.effect or 0) >= min_eff]
    if len(days) >= min_days:
        peak = max(s.effect for s in days)
        c.notes.append(
            f"L2 drift route: OFF_TARGET_RTP persisted {len(days)} cell-days "
            f"(peak +{peak:.1f} RTP points vs certified) -- game/platform-level "
            f"drift, not a player finding")
        return True
    return False


def fuse(cfg: Settings, signals: list[Signal],
         *,
         cohort_membership: dict[tuple[str, str], str] | None = None,
         population_tested: int | None = None) -> list[Candidate]:
    from ..paths import game_case_id, player_case_id  # noqa: PLC0415
    fus = cfg.thresholds.get("fusion", {})
    cohort_membership = cohort_membership or {}

    # ---- group ----------------------------------------------------------- #
    by_entity: dict[tuple, Candidate] = {}
    for s in signals:
        if s.grain == "game_cell":
            key = ("G", s.game_id, s.play_type, s.currency, s.sm_tag)
            if key not in by_entity:
                by_entity[key] = Candidate(
                    case_id=game_case_id(s.game_id or "NA", s.play_type,
                                         s.sm_tag,
                                         s.context.get("event_date", "window")),
                    grain="game_cell", parent=None, uid=None,
                    game_id=s.game_id, play_type=s.play_type,
                    currency=s.currency, sm_tag=s.sm_tag)
        else:
            key = ("P", s.parent, s.uid)
            if key not in by_entity:
                by_entity[key] = Candidate(
                    case_id=player_case_id(s.parent or "NA", s.game_id, s.uid or "NA"),
                    grain="player", parent=s.parent, uid=s.uid,
                    game_id=s.game_id, play_type=s.play_type,
                    currency=s.currency, sm_tag=s.sm_tag)
        c = by_entity[key]
        c.signals.append(s)
        c.families.add(s.family)

    # ---- score, gate, escalate ------------------------------------------- #
    out: list[Candidate] = []
    emz = expected_max_z(population_tested) if population_tested else None
    for c in by_entity.values():
        contributing = c.families - NON_CONTRIBUTING_FAMILIES
        c.independent_families = len(contributing)
        c.expected_max_z = emz

        if c.grain == "player" and c.parent and c.uid:
            c.cohort_id = cohort_membership.get((c.parent, c.uid))
            if c.cohort_id:
                c.notes.append(
                    f"member of detected cohort {c.cohort_id} (unapproved -- "
                    f"NOT suppressed): report inclusive AND leave-cohort-out "
                    f"baselines side by side; see out/proposed_exclusions.yaml "
                    f"for competing explanations")

        persist_held = any(s.persistence == "held" for s in c.signals)
        if c.independent_families >= int(fus.get(
                "escalate_human_review_families", 2)):
            c.escalation = "human_review"
        elif (c.independent_families >= int(fus.get(
                "escalate_enhanced_families", 2)) - 1 and persist_held):
            c.escalation = "enhanced"
        elif c.independent_families >= 1:
            c.escalation = "monitor"
        else:
            c.escalation = "monitor"   # ENVIRONMENT-only: game-level context

        if c.escalation != "human_review" and (
                _integrity_route(c, cfg) or _drift_route(c, cfg)
                or _exceptional_route(c, cfg)):
            c.escalation = "human_review"

        # severity for the composer's SLA language
        integ_cert = any(s.family == "INTEGRITY"
                         and s.baseline_source in ("certified", "identity")
                         for s in c.signals)
        if c.escalation == "human_review" and integ_cert:
            c.severity = "Critical" if any(
                s.signal_id == "L1-MAX_X_BREACH" for s in c.signals) else "High"
        elif c.escalation == "human_review":
            c.severity = "High" if persist_held else "Medium"
        elif c.escalation == "enhanced":
            c.severity = "Medium"
        else:
            c.severity = "Low"

        if emz is not None:
            zs = [s.value for s in c.signals if s.p_value is not None]
            if zs and max(zs) < emz + 1:
                c.notes.append(
                    f"max z {max(zs):.1f} vs expected null maximum "
                    f"{emz:.1f} over {population_tested:,} tested -- "
                    f"consistent with a population maximum; weigh accordingly")
        out.append(c)

    rank = {"human_review": 0, "enhanced": 1, "monitor": 2}
    sev = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    out.sort(key=lambda c: (rank[c.escalation], sev[c.severity],
                            -c.independent_families,
                            -max((abs(s.value) for s in c.signals), default=0)))
    log.info("fuse: %d candidates (%d human_review, %d enhanced, %d monitor)",
             len(out), *(sum(1 for c in out if c.escalation == e)
                         for e in ("human_review", "enhanced", "monitor")))
    return out
