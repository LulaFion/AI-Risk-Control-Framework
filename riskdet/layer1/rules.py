"""Layer 1 -- explainable first-scan rules over the feature artifacts.

Composition:
  calibrated rules   the ROBUST/BINOMIAL metrics evaluated against FROZEN
                     parameters from calibrate.py (fit on May, validated on
                     June-July). Never re-fit at scan time.
  absolute rules     identities and contractual bounds that need no
                     calibration: BALANCE_IDENTITY, MAX_X_BREACH, DUP_ROUND.
  structural rules   DUTY_CYCLE (humans sleep), BETA_IN_PROD (game level).
  PRESCIENT_BET      exact permutation null from sufficient statistics --
                     under honest math the outcome of a round is independent
                     of the stake chosen for it, so mean(bet | triggered) is
                     distributed as a size-k sample WOR from the player's own
                     bet vector. No peer baseline, no certified sheet, immune
                     to fleet selection bias.

Persistence: where the underlying statistics are additive, the window is split
at its midpoint by subtracting cumulative extractions (rounds_asof(mid) from
rounds_asof(end)) and the effect must hold in BOTH halves. Where a statistic
is not additive (modal share, max-per-minute), persistence is recorded as
not_computable and the burden moves to Layer 4.

Every rule match is a REASON TO INVESTIGATE. Nothing here is a finding.
"""

from __future__ import annotations

import datetime as _dt
import logging

import numpy as np
import pandas as pd

from ..calibrate import (FrozenParams, MetricFrame, bh_select, evaluate,
                         robust_bound, z_to_p_onesided)
from ..config import LOG_RETENTION_DAYS, Settings
from ..filters import apply_exclusions
from ..refdata import GameCatalog
from ..signals import EvidenceRef, Signal

log = logging.getLogger("riskdet.layer1")

CELL_KEYS = ["game_id", "play_type", "currency", "sm_tag"]

# additive sufficient statistics -- safe to subtract for half-window persistence
_ADDITIVE_CELL = ["rounds", "turnover", "total_win", "n_win", "n_trigger",
                  "dup_seq_rounds", "sum_bet", "sumsq_bet", "sum_bet_trigger",
                  "sumsq_valid_bet", "sum_log_mult", "sumsq_log_mult",
                  "balance_violations"]
_ADDITIVE_TIMING = ["rounds", "n_gaps", "same_second_gaps", "in_range_gaps",
                    "concurrent_rounds"]


def subtract_additive(end: pd.DataFrame, mid: pd.DataFrame,
                      keys: list[str], additive: list[str]) -> pd.DataFrame:
    """Second-half stats = cumulative(end) - cumulative(mid). Non-additive
    columns are intentionally absent from the result."""
    cols = [c for c in additive if c in end.columns and c in mid.columns]
    m = end[keys + cols].merge(mid[keys + cols], on=keys, how="left",
                               suffixes=("", "_mid"))
    for c in cols:
        m[c] = m[c] - m[f"{c}_mid"].fillna(0)
    return m[keys + cols]


def _evidence(row, now_utc: _dt.datetime) -> tuple[EvidenceRef, ...]:
    wire = row.get("top_win_seq_ids") if hasattr(row, "get") else None
    if not isinstance(wire, str) or not wire:
        return ()
    return tuple(EvidenceRef.parse(w, now_utc=now_utc,
                                   retention_days=LOG_RETENTION_DAYS)
                 for w in wire.split("|") if w)


# --------------------------------------------------------------------------- #
def run_layer1(cfg: Settings, catalog: GameCatalog, frozen: FrozenParams,
               metrics: list[MetricFrame],
               player_cell: pd.DataFrame,
               player_timing: pd.DataFrame,
               cell_constants: pd.DataFrame,
               *,
               half_player_cell: pd.DataFrame | None = None,
               half_player_timing: pd.DataFrame | None = None,
               integrity_pc: pd.DataFrame | None = None,
               now_utc: _dt.datetime | None = None) -> list[Signal]:
    now_utc = now_utc or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    rules_cfg = cfg.thresholds["rules"]
    pc, _ = apply_exclusions(player_cell, cfg)
    pt, _ = apply_exclusions(player_timing, cfg)
    # Integrity rules (balance identity, duplicate round, max-multiple breach) are
    # ABSOLUTE correctness checks, not population statistics -- they must run on
    # every cell regardless of volume. If an ungated frame is supplied, use it so
    # low-volume integrity attacks below the min_rounds feature floor are still
    # caught; otherwise fall back to the (possibly gated) statistical frame.
    ipc = pc if integrity_pc is None else apply_exclusions(integrity_pc, cfg)[0]
    signals: list[Signal] = []

    # ---------------- calibrated metrics (frozen params) ------------------ #
    grain_by_metric = {
        "buy_share_turnover": "player", "same_second_share": "player",
        "modal_share": "player", "concurrent_rounds": "player",
        "max_rounds_per_minute": "player",
        "trigger_rate_z": "player_cell", "hit_rate_z": "player_cell",
        "excess_turnover_z": "player_cell",
    }
    for mf in metrics:
        fm = frozen.metrics.get(mf.metric)
        if fm is None or not fm.enabled:
            continue
        fired = evaluate(mf.df, fm)
        for _, row in mf.df[fired].iterrows():
            grain = grain_by_metric.get(mf.metric, "player")
            persistence = _persistence_for(
                mf.metric, row, fm, pc, pt,
                half_player_cell, half_player_timing)
            signals.append(Signal(
                signal_id=fm.rule, family=rules_cfg[fm.rule]["family"],
                layer=1, grain=grain,
                parent=row.get("parent"), uid=row.get("uid"),
                game_id=row.get("game_id"),
                play_type=(int(row["play_type"])
                           if "play_type" in row and pd.notna(row.get("play_type"))
                           else None),
                currency=row.get("currency"), sm_tag=row.get("sm_tag"),
                metric=mf.metric, value=float(row["value"]),
                threshold=fm.bound,
                threshold_method=("ROBUST(frozen)" if fm.kind == "ROBUST"
                                  else "BINOMIAL+BH(frozen q)"),
                baseline_source=("certified" if mf.metric == "excess_turnover_z"
                                 else "empirical_loo"),
                p_value=(float(z_to_p_onesided(np.array([row["value"]]))[0])
                         if fm.kind == "Z" else None),
                effect=(float(row["lift"]) if "lift" in row and pd.notna(row.get("lift"))
                        else (float(row["effect_rtp_points"])
                              if "effect_rtp_points" in row
                              and pd.notna(row.get("effect_rtp_points")) else None)),
                effect_unit=("lift" if "lift" in row else "rtp_points"),
                n=int(row["rounds"]) if "rounds" in row else None,
                persistence=persistence,
                description=(f"{mf.metric}={row['value']:.4g} vs frozen bound "
                             f"{fm.bound if fm.bound is not None else 'BH'}"),
                evidence=_evidence(row, now_utc)))

    # ---------------- PRESCIENCE: exact permutation null ------------------ #
    p_cfg = rules_cfg["L1-PRESCIENT_BET"]
    b = pc[(pc.play_type == 0)
           & (pc.rounds >= int(p_cfg.get("min_base_rounds", 2000)))
           & (pc.n_trigger >= int(p_cfg.get("min_trigger_events", 30)))
           & (pc.bet_levels > 1)].copy()
    if len(b):
        n = b.rounds.to_numpy(float)
        k = b.n_trigger.to_numpy(float)
        mean_all = b.sum_bet.to_numpy(float) / n
        var_pop = b.sumsq_bet.to_numpy(float) / n - mean_all ** 2
        mean_trig = b.sum_bet_trigger.to_numpy(float) / k
        var_null = (var_pop / k) * (n - k) / np.maximum(n - 1, 1)
        with np.errstate(divide="ignore", invalid="ignore"):
            z = (mean_trig - mean_all) / np.sqrt(var_null)
        valid = np.isfinite(z)
        sel = np.zeros(len(b), dtype=bool)
        sel[valid] = bh_select(z_to_p_onesided(z[valid]),
                               float(p_cfg.get("fdr_q", 0.01)))
        for i, (_, row) in enumerate(b.iterrows()):
            if not sel[i]:
                continue
            signals.append(Signal(
                signal_id="L1-PRESCIENT_BET", family="PRESCIENCE", layer=1,
                grain="player_cell", parent=row.parent, uid=row.uid,
                game_id=row.game_id, play_type=0, currency=row.currency,
                sm_tag=row.sm_tag, metric="prescient_bet_z",
                value=float(z[i]), threshold=None,
                threshold_method="PERMUTATION+BH(frozen q)",
                baseline_source="identity",
                p_value=float(z_to_p_onesided(np.array([z[i]]))[0]),
                effect=float(mean_trig[i] / mean_all[i]), effect_unit="bet_lift",
                n=int(row.rounds), persistence="not_evaluated",
                description=(f"stake on triggering rounds {mean_trig[i]:.4g} vs "
                             f"overall {mean_all[i]:.4g} (z={z[i]:.1f}): under "
                             f"honest math the outcome is independent of the "
                             f"stake chosen for the round"),
                evidence=_evidence(row, now_utc)))

    # ---------------- INTEGRITY: absolute rules (ungated) ----------------- #
    for _, row in ipc[ipc.balance_violations > 0].iterrows():
        signals.append(Signal(
            signal_id="L1-BALANCE_IDENTITY", family="INTEGRITY", layer=1,
            grain="player_cell", parent=row.parent, uid=row.uid,
            game_id=row.game_id, play_type=int(row.play_type),
            currency=row.currency, sm_tag=row.sm_tag,
            metric="balance_identity_violations",
            value=float(row.balance_violations), threshold=0.0,
            threshold_method="ABSOLUTE", baseline_source="identity",
            n=int(row.rounds), persistence="not_computable",
            description=(f"{int(row.balance_violations)} round(s) where "
                         f"after != before - bet + win (same-row identity; "
                         f"immune to deposits and session gaps)"),
            evidence=_evidence(row, now_utc)))

    for _, row in ipc[ipc.dup_seq_rounds > 0].iterrows():
        signals.append(Signal(
            signal_id="L1-DUP_ROUND", family="INTEGRITY", layer=1,
            grain="player_cell", parent=row.parent, uid=row.uid,
            game_id=row.game_id, play_type=int(row.play_type),
            currency=row.currency, sm_tag=row.sm_tag,
            metric="dup_seq_rounds", value=float(row.dup_seq_rounds),
            threshold=0.0, threshold_method="ABSOLUTE",
            baseline_source="identity", n=int(row.rounds),
            persistence="not_computable",
            description=("duplicate game_seq_id settled more than once -- "
                         "double-settle vs ETL duplicate is decidable ONLY by "
                         "the spin-server response count in Cloud Logging"),
            context={"requires_log_adjudication": True},
            evidence=_evidence(row, now_utc)))

    def _max_x(row) -> float | None:
        mx, _src = catalog.max_multiplier(row.game_id, int(row.play_type))
        return mx
    mm = ipc.copy()
    mm["cert_max_x"] = mm.apply(_max_x, axis=1)
    breach = mm[mm.cert_max_x.notna() & (mm.max_multiple > mm.cert_max_x)]
    for _, row in breach.iterrows():
        signals.append(Signal(
            signal_id="L1-MAX_X_BREACH", family="INTEGRITY", layer=1,
            grain="player_cell", parent=row.parent, uid=row.uid,
            game_id=row.game_id, play_type=int(row.play_type),
            currency=row.currency, sm_tag=row.sm_tag,
            metric="max_multiple", value=float(row.max_multiple),
            threshold=float(row.cert_max_x), threshold_method="ABSOLUTE",
            baseline_source="certified", n=int(row.rounds),
            persistence="not_computable",
            description=(f"round paid {row.max_multiple:.0f}x vs certified max "
                         f"{row.cert_max_x:.0f}x -- an exploit, a data error, "
                         f"or a wrong sheet; all three are findings"),
            evidence=_evidence(row, now_utc)))

    # ---------------- MAGNITUDE WATCH: sub-floor, volume-independent ------- #
    # Deterministic, explainable sub-floor watch: material money at a rate far
    # above certified. Kept alongside Layer-3 ML because ML's validation gate is
    # suppressed by concentrated cohorts and Isolation Forest is blind to cohorts
    # -- so this stable rule remains the reliable watch; ML is a supplement for
    # the isolated-outlier tail. Non-contributing family: monitor/watch only.
    floor = int(cfg.thresholds["global"]["min_rounds_per_cell"])
    w_cfg = rules_cfg.get("L1-MAGNITUDE_WATCH", {})
    n_mad = float(w_cfg.get("mad_mult", 8.0))
    min_pop = int(w_cfg.get("min_population", 50))
    min_rounds_watch = int(w_cfg.get("min_rounds", 10))   # anti-spike guard, NOT the sig floor
    w = ipc[(ipc.rounds < floor) & (ipc.rounds >= min_rounds_watch)
            & (ipc.turnover > 0)].copy()
    if len(w):
        uniq = w[["game_id", "play_type"]].drop_duplicates()
        uniq["cert_rtp"] = uniq.apply(
            lambda r: catalog.certified_rtp(r.game_id, int(r.play_type))[0], axis=1)
        w = w.merge(uniq, on=["game_id", "play_type"], how="left")
        w = w[w.cert_rtp.notna() & (w.cert_rtp > 0)]
    if len(w):
        w["obs_rtp"] = w.total_win / w.turnover
        w["rtp_ratio"] = w.obs_rtp / w.cert_rtp
        w["net_profit"] = w.total_win - w.turnover
        ratio_bound, _ = robust_bound(w.rtp_ratio.to_numpy(float), n_mad, min_pop)
        fired = []
        if ratio_bound is not None:
            for _cur, grp in w.groupby("currency"):
                pos = grp.net_profit[grp.net_profit > 0].to_numpy(float)
                net_bound, _ = robust_bound(pos, n_mad, min_pop)
                if net_bound is None:
                    continue
                fired.extend(grp[(grp.rtp_ratio >= ratio_bound)
                                 & (grp.net_profit >= net_bound)].index.tolist())
        for _, row in w.loc[fired].iterrows():
            signals.append(Signal(
                signal_id="L1-MAGNITUDE_WATCH", family="MAGNITUDE_WATCH", layer=1,
                grain="player_cell", parent=row.parent, uid=row.uid,
                game_id=row.game_id, play_type=int(row.play_type),
                currency=row.currency, sm_tag=row.sm_tag,
                metric="rtp_ratio", value=float(row.rtp_ratio),
                threshold=float(ratio_bound),
                threshold_method="ROBUST(pop ratio) + money materiality",
                baseline_source="certified", n=int(row.rounds),
                persistence="not_evaluated",
                effect=float(row.net_profit), effect_unit="net_currency",
                description=(f"RTP {row.obs_rtp:.0%} = {row.rtp_ratio:.1f}x certified "
                             f"{row.cert_rtp:.0%} on {int(row.rounds)} rounds (below the "
                             f"{floor}-round significance floor), net +{row.net_profit:,.0f} "
                             f"{row.currency}: material excess at a rate too high for the "
                             f"volume -- WATCH only, not a significance test; confirm via "
                             f"persistence across days or a round-level math check"),
                context={"below_significance_floor": True, "watch_only": True},
                evidence=_evidence(row, now_utc)))

    # ---------------- TIMING: structural duty cycle ----------------------- #
    d_cfg = rules_cfg["L1-DUTY_CYCLE"]
    dc = pt[(pt.hours_of_day_covered >= int(d_cfg.get("min_hours_covered", 22)))
            & (pt.active_days >= int(d_cfg.get("min_active_days", 7)))
            & (pt.max_idle_s.notna())
            & (pt.max_idle_s < int(d_cfg.get("max_idle_hours", 3)) * 3600)]
    for _, row in dc.iterrows():
        signals.append(Signal(
            signal_id="L1-DUTY_CYCLE", family="TIMING", layer=1,
            grain="player", parent=row.parent, uid=row.uid,
            game_id=None, play_type=None, currency=None, sm_tag=None,
            metric="hours_of_day_covered", value=float(row.hours_of_day_covered),
            threshold=float(d_cfg.get("min_hours_covered", 22)),
            threshold_method="STRUCTURAL", baseline_source="none",
            n=int(row.rounds), persistence="not_computable",
            description=(f"{int(row.hours_of_day_covered)}/24 hours active over "
                         f"{int(row.active_days)} days, max idle "
                         f"{row.max_idle_s/3600:.1f}h -- humans sleep; benign "
                         f"alternatives: shared terminal, rented account")))

    # ---------------- ENVIRONMENT: game-level beta ------------------------ #
    if "beta_rounds" in cell_constants.columns:
        for _, row in cell_constants[cell_constants.beta_rounds > 0].iterrows():
            signals.append(Signal(
                signal_id="L1-BETA_IN_PROD", family="ENVIRONMENT", layer=1,
                grain="game_cell", parent=None, uid=None,
                game_id=row.game_id, play_type=int(row.play_type),
                currency=row.currency, sm_tag=row.sm_tag,
                metric="beta_rounds", value=float(row.beta_rounds),
                threshold=0.0, threshold_method="ABSOLUTE",
                baseline_source="none", n=int(row.rounds),
                persistence="not_computable",
                description=("feature_beta math served real-money rounds -- a "
                             "build attribute shared by every player on it; "
                             "never counts toward any player's signals")))

    log.info("layer1: %d signals", len(signals))
    return signals


# --------------------------------------------------------------------------- #
def _persistence_for(metric: str, row, fm, pc, pt,
                     half_pc: pd.DataFrame | None,
                     half_pt: pd.DataFrame | None) -> str:
    """Both-halves check on additive statistics. Direction + nominal
    significance (p<0.05) must hold in each half independently."""
    if metric in ("modal_share", "max_rounds_per_minute", "concurrent_rounds",
                  "buy_share_turnover"):
        return "not_computable"
    if metric == "same_second_share":
        if half_pt is None:
            return "not_evaluated"
        key = ["parent", "uid"]
        end = pt[(pt.parent == row.parent) & (pt.uid == row.uid)]
        mid = half_pt[(half_pt.parent == row.parent) & (half_pt.uid == row.uid)]
        if end.empty or mid.empty:
            return "not_computable"
        h2 = subtract_additive(end, mid, key, _ADDITIVE_TIMING).iloc[0]
        h1 = mid.iloc[0]
        r1 = h1.same_second_gaps / max(h1.n_gaps, 1)
        r2 = h2.same_second_gaps / max(h2.n_gaps, 1)
        thr = fm.bound if fm.bound is not None else np.inf
        return "held" if (r1 > thr and r2 > thr) else "failed"
    if metric in ("trigger_rate_z", "hit_rate_z", "excess_turnover_z"):
        if half_pc is None:
            return "not_evaluated"
        keys = ["parent", "uid"] + CELL_KEYS
        sel = ((pc.parent == row.parent) & (pc.uid == row.uid)
               & (pc.game_id == row.game_id) & (pc.play_type == row.play_type)
               & (pc.currency == row.currency) & (pc.sm_tag == row.sm_tag))
        end = pc[sel]
        selm = ((half_pc.parent == row.parent) & (half_pc.uid == row.uid)
                & (half_pc.game_id == row.game_id)
                & (half_pc.play_type == row.play_type)
                & (half_pc.currency == row.currency)
                & (half_pc.sm_tag == row.sm_tag))
        mid = half_pc[selm]
        if end.empty or mid.empty:
            return "not_computable"
        h2 = subtract_additive(end, mid, keys, _ADDITIVE_CELL).iloc[0]
        h1 = mid.iloc[0]

        def half_ok(h) -> bool:
            n = max(float(h.rounds), 1.0)
            if metric in ("trigger_rate_z", "hit_rate_z"):
                # per-half rate against the FROZEN full-window LOO peer rate;
                # each half must clear nominal one-sided significance on its own
                p0 = row.get("p0_frozen", None)
                if p0 is None or not np.isfinite(p0):
                    return False
                num = h.n_trigger if metric == "trigger_rate_z" else h.n_win
                z = (num / n - p0) / np.sqrt(p0 * (1 - p0) / n)
                return z >= 1.645
            # excess_turnover_z
            sig = row.get("sigma_spin", None)
            cert = row.get("rtp_cert", None)
            if sig is None or cert is None or h.sumsq_valid_bet <= 0:
                return False
            z = ((h.total_win - cert * h.turnover)
                 / (sig * np.sqrt(h.sumsq_valid_bet)))
            return z >= 1.645

        return "held" if (half_ok(h1) and half_ok(h2)) else "failed"
    return "not_evaluated"
