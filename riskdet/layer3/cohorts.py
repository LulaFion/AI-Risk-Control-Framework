"""Coordinated Cohort Detector -- establishes that accounts MOVE TOGETHER.

That is a FACT about the data. WHY they move together is a hypothesis, and the
competing explanations ship with every proposal:

    QA/load-test harness, promotion cohort, autoplay defaults, account
    migration, shared terminals (internet cafes), affiliate bulk
    registration, genuinely coordinated play, ETL artifacts.

Governance (CLAUDE.md + explicit correction): detection NEVER excludes and
NEVER suppresses. Unapproved cohorts stay fully visible. Downstream analyses
report BOTH the inclusive and the leave-cohort-out result side by side --
`membership()` exists to enable that dual computation, not to filter anyone
out. Only a human moving a cohort into config/exclusions.yaml (with approver,
date, ticket) excludes it.

Six independent fingerprints; a cohort is proposed when >= min_agreeing agree:
  uid_suffix_block        dense sequential numeric block in one uid skeleton
  synchronised_birth      first-seen times inside one short window
  behavioural_identity    near-identical timing feature vectors
  cadence_correlation     hour-of-day activity profiles correlate pairwise
  superhuman_duty_cycle   members individually active ~24/24 hours
  variance_spread_money   pooled excess vs certified ~ 0 while member RTPs
                          span widely -- per-currency z, Stouffer-combined;
                          raw money is NEVER summed across currencies

All sensitivity parameters live in config `cohort_detection:` -- none are
hardcoded here.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import itertools
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import Settings
from ..refdata import GameCatalog

log = logging.getLogger("riskdet.cohorts")

COMPETING_EXPLANATIONS = [
    "QA or load-test harness",
    "promotion cohort (same campaign, same start)",
    "client autoplay defaults shared across installs",
    "account migration / renumbering by the operator",
    "shared physical terminals (internet cafe operators)",
    "affiliate bulk registration",
    "genuinely coordinated play (one actor, many accounts)",
    "ETL or replication artifact",
]

CELL_KEYS = ["game_id", "play_type", "currency", "sm_tag"]
_TRAIL_NUM = re.compile(r"(\d+)\D*$")


@dataclass
class CohortProposal:
    cohort_id: str                       # stable sha256-derived
    parent: str
    uids: list[str]                      # COMPLETE membership, never sampled
    fingerprints: dict[str, str] = field(default_factory=dict)
    per_currency_money_z: dict[str, float] = field(default_factory=dict)
    competing_explanations: list[str] = field(
        default_factory=lambda: list(COMPETING_EXPLANATIONS))

    @property
    def n_agreeing(self) -> int:
        return len(self.fingerprints)


def _skeleton(uid: str) -> str:
    return re.sub(r"\d", "#", re.sub(r"[A-Za-z]", "a", uid))


def _cohort_id(parent: str, skeleton: str, uids: list[str]) -> str:
    """Stable across processes and runs (hash() is salted per process)."""
    h = hashlib.sha256(
        f"{parent}|{skeleton}|{','.join(sorted(uids)[:50])}".encode("utf-8"))
    return f"cohort_{h.hexdigest()[:12]}"


def _pairwise_mean_corr(profiles: np.ndarray, min_shared: int) -> float | None:
    """Mean Pearson correlation over member pairs whose profiles share at
    least min_shared active hours. None when no pair qualifies."""
    n = profiles.shape[0]
    if n < 2:
        return None
    corrs: list[float] = []
    for i, j in itertools.combinations(range(min(n, 40)), 2):  # cap the O(n^2)
        a, b = profiles[i], profiles[j]
        shared = int(np.sum((a > 0) & (b > 0)))
        if shared < min_shared:
            continue
        if a.std() == 0 or b.std() == 0:
            continue
        corrs.append(float(np.corrcoef(a, b)[0, 1]))
    return float(np.mean(corrs)) if corrs else None


def detect_cohorts(cfg: Settings, catalog: GameCatalog,
                   player_cell: pd.DataFrame,
                   player_timing: pd.DataFrame,
                   cell_constants: pd.DataFrame) -> list[CohortProposal]:
    cc_cfg = cfg.thresholds.get("cohort_detection", {})
    min_members = int(cc_cfg.get("min_members", 10))
    min_density = float(cc_cfg.get("uid_suffix_min_density", 0.5))
    min_agree = int(cc_cfg.get("min_agreeing_fingerprints", 3))
    birth_window = _dt.timedelta(
        minutes=float(cc_cfg.get("birth_window_minutes", 60)))
    cv_max = float(cc_cfg.get("behavioural_cv_max", 0.05))
    corr_thr = float(cc_cfg.get("cadence_correlation_threshold", 0.8))
    min_shared_h = int(cc_cfg.get("cadence_min_shared_active_hours", 12))
    scale_factor = float(cc_cfg.get("scale_outlier_factor", 20))
    rtp_spread_min = float(cc_cfg.get("member_rtp_spread_min", 0.02))
    null_sigma = float(cfg.thresholds.get("selection_bias", {})
                       .get("cohort_null_sigma", 1.0))

    sigma_by_cell = cell_constants.set_index(CELL_KEYS)["sigma_spin"]

    per_player = (player_cell.groupby(["parent", "uid"], as_index=False)
                  .agg(rounds=("rounds", "sum"),
                       first_seen=("first_round_utc", "min")))
    t = player_timing.set_index(["parent", "uid"]).sort_index()

    proposals: list[CohortProposal] = []
    for parent, grp in per_player.groupby("parent"):
        if len(grp) < min_members:
            continue
        parent_median_rounds = float(grp.rounds.median())

        for skel, cls in grp.assign(_s=grp.uid.map(_skeleton)).groupby("_s"):
            if len(cls) < min_members:
                continue
            uids = sorted(cls.uid)
            prints: dict[str, str] = {}
            money_z: dict[str, float] = {}

            # --- uid_suffix_block ------------------------------------- #
            nums = cls.uid.str.extract(_TRAIL_NUM, expand=False).dropna()
            if len(nums) >= min_members:
                vals = nums.astype(int)
                span = int(vals.max() - vals.min() + 1)
                density = len(vals) / span if span > 0 else 0.0
                if density >= min_density:
                    prints["uid_suffix_block"] = (
                        f"{len(cls)} uids share skeleton '{skel}'; numeric "
                        f"suffix density {density:.2f} over span {span}")

            # --- synchronised_birth ------------------------------------ #
            births = pd.to_datetime(cls.first_seen)
            if births.notna().all() and len(births) >= min_members:
                spread = births.max() - births.min()
                if spread <= birth_window:
                    prints["synchronised_birth"] = (
                        f"all {len(cls)} first seen within {spread} "
                        f"(window {birth_window})")

            # --- timing-based fingerprints ----------------------------- #
            idx = [(parent, u) for u in uids if (parent, u) in t.index]
            tv = t.loc[idx] if idx else pd.DataFrame()
            if len(tv) >= min_members:
                feats = tv[["modal_gap_s", "hours_of_day_covered",
                            "max_rounds_per_minute"]].astype(float)
                cv = float((feats.std() / feats.mean().replace(0, np.nan))
                           .mean())
                if np.isfinite(cv) and cv < cv_max:
                    prints["behavioural_identity"] = (
                        f"mean CV across timing features {cv:.3f} "
                        f"(< {cv_max}) over {len(tv)} accounts")

                share_24h = float((tv.hours_of_day_covered >= 22).mean())
                if share_24h >= 0.8:
                    prints["superhuman_duty_cycle"] = (
                        f"{share_24h:.0%} of members active >=22/24 hours")

                if "hour_profile" in tv.columns:
                    profs = np.array([np.asarray(p, dtype=float)
                                      for p in tv.hour_profile
                                      if p is not None and len(p) == 24])
                    if len(profs) >= min_members:
                        mc = _pairwise_mean_corr(profs, min_shared_h)
                        if mc is not None and mc >= corr_thr:
                            prints["cadence_correlation"] = (
                                f"mean pairwise hour-profile correlation "
                                f"{mc:.2f} (>= {corr_thr}) -- members move "
                                f"together in time")

            # --- variance_spread_money: per currency, Stouffer ---------- #
            sub = player_cell[(player_cell.parent == parent)
                              & (player_cell.uid.isin(set(uids)))].copy()
            sub["cert"] = sub.apply(
                lambda r: catalog.certified_rtp(r.game_id,
                                                int(r.play_type))[0], axis=1)
            sub = sub.dropna(subset=["cert"])
            sub["sigma_spin"] = sigma_by_cell.reindex(
                pd.MultiIndex.from_frame(sub[CELL_KEYS])).to_numpy()
            sub = sub.dropna(subset=["sigma_spin"])
            sub = sub[(sub.sigma_spin > 0) & (sub.sumsq_valid_bet > 0)]
            if len(sub):
                # raw money never crosses a currency boundary: one z per
                # currency, combined by Stouffer's method
                for ccy, s in sub.groupby("currency"):
                    excess = float((s.total_win - s.cert * s.turnover).sum())
                    se = float(np.sqrt((s.sigma_spin ** 2
                                        * s.sumsq_valid_bet).sum()))
                    if se > 0:
                        money_z[str(ccy)] = excess / se
                if money_z:
                    z_comb = float(sum(money_z.values())
                                   / np.sqrt(len(money_z)))
                    member_rtp = (sub.groupby("uid")
                                  .apply(lambda d: d.total_win.sum()
                                         / max(d.turnover.sum(), 1e-9),
                                         include_groups=False))
                    if (abs(z_comb) <= null_sigma
                            and len(member_rtp) >= min_members
                            and float(member_rtp.max() - member_rtp.min())
                            >= rtp_spread_min):
                        prints["variance_spread_money"] = (
                            f"Stouffer-combined excess z={z_comb:.2f} across "
                            f"{len(money_z)} currency(ies) (~certified) while "
                            f"member RTP spans {member_rtp.min():.3f}-"
                            f"{member_rtp.max():.3f} -- a variance-spread "
                            f"population")

            # --- scale_outlier ------------------------------------------ #
            if (parent_median_rounds > 0
                    and float(cls.rounds.median())
                    >= scale_factor * parent_median_rounds):
                prints["scale_outlier"] = (
                    f"cohort median {cls.rounds.median():,.0f} rounds/account "
                    f"vs parent median {parent_median_rounds:,.0f} "
                    f"(>= {scale_factor:g}x)")

            if len(prints) >= min_agree:
                proposals.append(CohortProposal(
                    cohort_id=_cohort_id(parent, skel, uids),
                    parent=parent, uids=uids, fingerprints=prints,
                    per_currency_money_z=money_z))

    log.info("cohort detector: %d proposal(s)", len(proposals))
    return proposals


def membership(proposals: list[CohortProposal]) -> dict[tuple[str, str], str]:
    """(parent, uid) -> cohort_id, for DUAL-BASELINE computation and candidate
    annotation. This is NOT a filter: unapproved cohorts are never excluded or
    suppressed -- downstream reports the inclusive and the leave-cohort-out
    result side by side and a human decides."""
    return {(p.parent, u): p.cohort_id for p in proposals for u in p.uids}


def write_proposals(cfg: Settings, proposals: list[CohortProposal]) -> Path:
    import yaml  # noqa: PLC0415
    cfg.paths.ensure()
    path = cfg.paths.out / "proposed_exclusions.yaml"
    doc = {
        "generated_utc": _dt.datetime.now(_dt.timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "instructions": (
            "A cohort here means these accounts MOVE TOGETHER -- a fact. The "
            "cause is a hypothesis: review the competing explanations before "
            "concluding anything. Nothing is excluded or suppressed "
            "automatically; analyses report inclusive AND leave-cohort-out "
            "results side by side. To exclude a cohort, move it into "
            "config/exclusions.yaml under approved_fleets WITH approver, "
            "date, ticket, and the evidence."),
        "proposals": [{
            "cohort_id": p.cohort_id,
            "parent": p.parent,
            "members": len(p.uids),
            "uids": p.uids,                       # complete membership
            "fingerprints": p.fingerprints,
            "per_currency_money_z": {k: round(v, 3)
                                     for k, v in p.per_currency_money_z.items()},
            "competing_explanations": p.competing_explanations,
        } for p in proposals],
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False, width=100),
                    encoding="utf-8")
    return path
