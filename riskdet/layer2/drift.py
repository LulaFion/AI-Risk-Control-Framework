"""Layer 2 -- game-level drift over the daily series.

Question answered: did the GAME (a peer cell) start paying differently --
deploy regression, trigger-logic fault, or configuration error -- as opposed to
Layer 1's per-player question. Grain: (game_id, play_type, currency, sm_tag)
per UTC event date, from 30_game_day_series.sql.

Methods (all on analytic nulls; no fitted time-series model until the history
and its seasonality have been validated -- an ARIMA fit on a contaminated or
short series is worse than a well-formed z):

  OFF_TARGET_RTP    day excess vs CERTIFIED RTP, turnover-weighted with the
                    unequal-stake SE (sigma_spin * sqrt(sumsq_valid_bet)),
                    BH-FDR over all cell-days tested, plus an effect floor.
                    Absolute test -- catches "wrong since day one".
  TRIGGER_DRIFT     day trigger rate vs the cell's OTHER days (leave-day-out),
                    binomial z, BH-FDR. A trigger break with normal RTP points
                    at config, not variance.
  DEPLOY_SHIFT      first N days of a new sm_tag vs the previous sm_tag's
                    pooled rate/RTP -- the build-vs-build step the per-build
                    grain would otherwise hide. Names the rollback target.

Zero findings on clean data is the expected outcome; nothing here is tuned to
a quantile of the scan population.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..calibrate import bh_select, z_to_p_onesided
from ..config import Settings
from ..refdata import GameCatalog
from ..signals import Signal

log = logging.getLogger("riskdet.layer2")

CELL_KEYS = ["game_id", "play_type", "currency", "sm_tag"]


def _mk_signal(row, *, signal_id: str, metric: str, value: float,
               method: str, baseline: str, p: float | None,
               effect: float | None, unit: str, desc: str) -> Signal:
    return Signal(
        signal_id=signal_id, family=("ENVIRONMENT" if "DEPLOY" in signal_id
                                     else "OUTCOME_MAGNITUDE"
                                     if "RTP" in signal_id
                                     else "OUTCOME_FREQUENCY"),
        layer=2, grain="game_cell",
        parent=None, uid=None,
        game_id=row.game_id, play_type=int(row.play_type),
        currency=row.currency, sm_tag=row.sm_tag,
        metric=metric, value=float(value), threshold=None,
        threshold_method=method, baseline_source=baseline,
        p_value=p, effect=effect, effect_unit=unit,
        n=int(row.rounds), persistence="not_evaluated",
        description=desc,
        context={"event_date": str(getattr(row, "event_date", ""))})


def run_layer2(cfg: Settings, catalog: GameCatalog,
               game_day: pd.DataFrame,
               min_day_rounds: int = 500,
               fdr_q: float = 0.01,
               effect_floor_rtp_points: float = 2.0,   # warn from +2 RTP points
               min_trigger_lift: float = 2.0) -> list[Signal]:
    gd = game_day[game_day.rounds >= min_day_rounds].copy()
    if gd.empty:
        return []
    signals: list[Signal] = []

    # ---- OFF_TARGET_RTP: absolute, vs the certified sheet ---------------- #
    gd["rtp_cert"] = gd.apply(
        lambda r: catalog.certified_rtp(r.game_id, int(r.play_type))[0], axis=1)
    # per-cell sigma_spin proxy from the cell's own daily dispersion is biased;
    # use the day's internal variance bound: sigma estimated from the pooled
    # cell across days (leave-day-out) via total sums.
    cell = gd.groupby(CELL_KEYS).agg(
        c_win=("total_win", "sum"), c_turn=("turnover", "sum"),
        c_rounds=("rounds", "sum"), c_days=("event_date", "nunique"),
        c_trig=("n_trigger", "sum")).reset_index()
    gd = gd.merge(cell, on=CELL_KEYS, how="left")

    t = gd[gd.rtp_cert.notna() & (gd.sumsq_valid_bet > 0)].copy()
    if len(t):
        # day-level sigma_spin is not observable from daily sums; use a
        # conservative slot-like floor inferred from the cell: sd of daily
        # RTP scaled back to round grain. sigma_round ~= sd(day_rtp)*sqrt(n_day)
        day_rtp = t.total_win / t.turnover.replace(0, np.nan)
        sd_by_cell = (t.assign(day_rtp=day_rtp)
                      .groupby(CELL_KEYS)["day_rtp"].transform("std"))
        n_day = t.rounds.astype(float)
        # recover the per-ROUND dispersion from the daily series, then scale it
        # to the DAY TOTAL with the unequal-stake weight:
        #   sigma_round ~= sd(day_rtp) * sqrt(n_day)
        #   SE(day excess) = sigma_round * sqrt(sumsq_valid_bet)
        # (an extra /sqrt(n) here understates the day-total SE by sqrt(n) and
        # makes every clean day "significant" -- caught by the synthetic test)
        sigma_round = (sd_by_cell * np.sqrt(n_day)).replace(0, np.nan)
        excess = t.total_win - t.rtp_cert * t.turnover
        se = sigma_round * np.sqrt(t.sumsq_valid_bet)
        z = (excess / se).replace([np.inf, -np.inf], np.nan)
        eff = 100.0 * excess / t.turnover.replace(0, np.nan)
        valid = z.notna() & (t.c_days >= 5)
        sel = np.zeros(len(t), dtype=bool)
        if valid.any():
            sel[valid.to_numpy()] = bh_select(
                z_to_p_onesided(z[valid].to_numpy()), fdr_q)
        mask = pd.Series(sel, index=t.index) & (eff >= effect_floor_rtp_points)
        for _, row in t[mask].iterrows():
            e = 100.0 * (row.total_win - row.rtp_cert * row.turnover) / row.turnover
            signals.append(_mk_signal(
                row, signal_id="L2-OFF_TARGET_RTP", metric="day_excess_rtp_points",
                value=e, method="ABSOLUTE+BH", baseline="certified",
                p=None, effect=e, unit="rtp_points",
                desc=(f"{row.event_date}: day RTP "
                      f"{row.total_win/row.turnover:.4f} vs certified "
                      f"{row.rtp_cert:.4f} over {int(row.rounds):,} rounds")))

    # ---- TRIGGER_DRIFT: leave-day-out binomial --------------------------- #
    g = gd[gd.c_rounds > gd.rounds].copy()
    if len(g):
        p0 = ((g.c_trig - g.n_trigger)
              / (g.c_rounds - g.rounds)).clip(1e-9, 1 - 1e-9)
        phat = g.n_trigger / g.rounds
        z = (phat - p0) / np.sqrt(p0 * (1 - p0) / g.rounds)
        lift = phat / p0
        sel = bh_select(z_to_p_onesided(z.to_numpy()), fdr_q)
        mask = pd.Series(sel, index=g.index) & (lift >= min_trigger_lift) \
               & (g.c_days >= 5)
        for _, row in g[mask].iterrows():
            i = row.name
            signals.append(_mk_signal(
                row, signal_id="L2-TRIGGER_DRIFT", metric="day_trigger_z",
                value=float(z.loc[i]), method="BINOMIAL+BH",
                baseline="empirical_loo",
                p=float(z_to_p_onesided(np.array([z.loc[i]]))[0]),
                effect=float(lift.loc[i]), unit="lift",
                desc=(f"{row.event_date}: trigger rate "
                      f"{row.n_trigger/row.rounds:.4%} vs cell baseline "
                      f"{p0.loc[i]:.4%} ({lift.loc[i]:.1f}x)")))

    # ---- DEPLOY_SHIFT: new build vs previous build ------------------------ #
    build = (gd.groupby(CELL_KEYS)
               .agg(first_day=("event_date", "min"), rounds=("rounds", "sum"),
                    win=("total_win", "sum"), turn=("turnover", "sum"),
                    ssvb=("sumsq_valid_bet", "sum"),
                    trig=("n_trigger", "sum"))
               .reset_index()
               .sort_values(["game_id", "play_type", "currency", "first_day"]))
    grp = build.groupby(["game_id", "play_type", "currency"])
    build["prev_tag"] = grp["sm_tag"].shift()
    build["prev_rtp"] = (grp.apply(lambda d: d.win / d.turn, include_groups=False)
                         .reset_index(level=[0, 1, 2], drop=True).shift())
    b = build[build.prev_tag.notna() & (build.rounds >= 5 * 500)].copy()
    if len(b):
        rtp = b.win / b.turn
        # variance of the new build's RTP around the previous build's level
        day_sd = 0.02  # conservative: 2 RTP points daily sd floor
        z = (rtp - b.prev_rtp) / (day_sd / np.sqrt(b.rounds / 500))
        big = b[(np.abs(rtp - b.prev_rtp) >= effect_floor_rtp_points / 100)
                & (np.abs(z) >= 4)]
        for _, row in big.iterrows():
            r_new = row.win / row.turn
            row = row.rename({"first_day": "event_date"})
            signals.append(_mk_signal(
                row, signal_id="L2-DEPLOY_SHIFT", metric="build_rtp_shift",
                value=float(r_new - row.prev_rtp), method="ABSOLUTE",
                baseline="empirical_build",
                p=None, effect=100 * float(r_new - row.prev_rtp),
                unit="rtp_points",
                desc=(f"build {row.sm_tag} pays {r_new:.4f} vs previous build "
                      f"{row.prev_tag} at {row.prev_rtp:.4f} -- rollback "
                      f"target is {row.prev_tag}")))

    log.info("layer2: %d signals", len(signals))
    return signals
