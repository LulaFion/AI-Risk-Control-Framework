"""Signal and evidence records -- the shared vocabulary of Layers 1-3 and the
fusion stage. Every number a report quotes traces back through these fields to
a query job or a stated method (CLAUDE.md's reproducibility requirement).
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

# Families drive independence counting: fusion counts DISTINCT FAMILIES, never
# rules, because rules inside one family are correlated by construction
# (all TIMING rules are functions of one gap series; RTP equals hit rate times
# conditional win size; and so on).
FAMILIES = ("TIMING", "ROUTING", "PRESCIENCE", "OUTCOME_FREQUENCY",
            "OUTCOME_MAGNITUDE", "INTEGRITY", "ENVIRONMENT", "ML_DISCOVERY")

# ENVIRONMENT describes the build/game, is shared by every player on it, and
# therefore never contributes to a player's independent-signal count.
# ML_DISCOVERY means "unusual", not "risky" -- alone it can only ever mean
# Monitor, and it is emitted solely for players NO rule flagged (an ML flag on
# a rule-flagged player is correlated by construction with the rule).
# MAGNITUDE_WATCH (sub-floor magnitude) and ML_DISCOVERY ("unusual", not risky)
# can only ever mean Watch/monitor and must never combine to force escalation.
NON_CONTRIBUTING_FAMILIES = frozenset({"ENVIRONMENT", "MAGNITUDE_WATCH"})


@dataclass(frozen=True)
class EvidenceRef:
    """One round an investigator can pull logs for: gsid@UTC, +/-1 min."""
    game_seq_id: str
    game_time_utc: _dt.datetime | None
    key: str                      # 'gsid@2026-08-14T03:11:07Z' wire format
    log_retention_ok: bool        # within the acp-prod 30-day log window?

    @classmethod
    def parse(cls, wire: str, *, now_utc: _dt.datetime,
              retention_days: int) -> "EvidenceRef":
        gsid, _, ts = wire.partition("@")
        when: _dt.datetime | None = None
        ok = False
        if ts:
            try:
                when = _dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
                ok = (now_utc - when) <= _dt.timedelta(days=retention_days)
            except ValueError:
                pass
        return cls(game_seq_id=gsid, game_time_utc=when, key=wire,
                   log_retention_ok=ok)


@dataclass(frozen=True)
class Signal:
    """One rule firing for one entity. A signal is a REASON TO INVESTIGATE,
    never a finding -- findings exist only after Layer 4 and skeptic review."""
    signal_id: str                # 'L1-TRIGGER_RATE'
    family: str                   # one of FAMILIES
    layer: int
    grain: str                    # 'player' | 'player_cell' | 'game_cell'
    parent: str | None
    uid: str | None
    game_id: str | None
    play_type: int | None
    currency: str | None
    sm_tag: str | None
    metric: str
    value: float
    threshold: float | None
    threshold_method: str         # ROBUST | BINOMIAL+BH | PERMUTATION+BH |
                                  # ABSOLUTE | STRUCTURAL
    baseline_source: str          # certified | certified_game_level |
                                  # empirical_loo | identity | none
    p_value: float | None = None
    effect: float | None = None   # method-specific practical effect size
    effect_unit: str = ""
    n: int | None = None
    persistence: str = "not_evaluated"   # held | failed | not_computable
    description: str = ""
    evidence: tuple[EvidenceRef, ...] = ()
    context: dict = field(default_factory=dict)

    @property
    def entity(self) -> tuple:
        return (self.parent, self.uid) if self.grain != "game_cell" else (
            self.game_id, self.play_type, self.currency, self.sm_tag)
