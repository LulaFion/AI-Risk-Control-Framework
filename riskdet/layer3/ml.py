"""Layer 3 ML -- unsupervised discovery of patterns the rules did not anticipate.

Model: IsolationForest over per-player behavioural features, per-peer-cell
z-normalised first (the approach validated in the parent project's
analyze_anomaly.py: normalise within game so cross-game scale differences do
not masquerade as anomalies, then one global model).

Honest framing, enforced in code:
  - "anomalous" means UNUSUAL, not risky. The model's output NEVER escalates
    anything by itself.
  - Because the features are Layer 1's features, an ML flag on a player a rule
    already flagged is CORRELATED-BY-CONSTRUCTION and adds nothing: signals
    are emitted ONLY for ML-DISCOVERY candidates -- players no rule flagged.
  - CLAUDE.md's validation gate: before any output enters the candidate list,
    the flag rate must be stable across game, currency, sm_tag, operator and
    volume segments; a model that concentrates its flags in one segment is
    modelling the segment, not anomaly, and is rejected (signals suppressed,
    verdict recorded).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..config import Settings
from ..signals import Signal

log = logging.getLogger("riskdet.ml")

FEATURES = ["buy_share_turnover", "trigger_rate", "hit_rate", "rtp",
            "same_second_share", "modal_share", "hours_of_day_covered",
            "log_rounds", "bet_levels", "balance_violations"]


@dataclass
class MLValidation:
    verdict: str                      # PASS | FAIL
    flag_rate: float
    segment_rates: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def _feature_frame(player_cell: pd.DataFrame,
                   player_timing: pd.DataFrame) -> pd.DataFrame:
    pc = player_cell
    agg = pc.groupby(["parent", "uid"], as_index=False).agg(
        rounds=("rounds", "sum"), turnover=("turnover", "sum"),
        total_win=("total_win", "sum"), n_win=("n_win", "sum"),
        n_trigger=("n_trigger", "sum"), bet_levels=("bet_levels", "max"),
        balance_violations=("balance_violations", "sum"),
        game_id=("game_id", "first"), currency=("currency", "first"),
        sm_tag=("sm_tag", "first"))
    buys = (pc[pc.play_type.isin({2, 3, 4})]
            .groupby(["parent", "uid"], as_index=False)
            .agg(buy_turnover=("turnover", "sum")))
    f = agg.merge(buys, on=["parent", "uid"], how="left")
    f["buy_turnover"] = f.buy_turnover.fillna(0.0)
    f["buy_share_turnover"] = f.buy_turnover / f.turnover.replace(0, np.nan)
    f["trigger_rate"] = f.n_trigger / f.rounds
    f["hit_rate"] = f.n_win / f.rounds
    f["rtp"] = f.total_win / f.turnover.replace(0, np.nan)
    f["log_rounds"] = np.log10(f.rounds.clip(lower=1))

    t = player_timing[["parent", "uid", "n_gaps", "same_second_gaps",
                       "modal_share", "hours_of_day_covered"]]
    f = f.merge(t, on=["parent", "uid"], how="left")
    f["same_second_share"] = (f.same_second_gaps
                              / f.n_gaps.replace(0, np.nan)).fillna(0)
    f["modal_share"] = f.modal_share.fillna(0)
    f["hours_of_day_covered"] = f.hours_of_day_covered.fillna(0)
    return f


def run_ml_discovery(cfg: Settings,
                     player_cell: pd.DataFrame,
                     player_timing: pd.DataFrame,
                     rule_flagged: set[tuple[str, str]],
                     contamination: float = 0.005,
                     random_state: int = 7
                     ) -> tuple[list[Signal], MLValidation]:
    from sklearn.ensemble import IsolationForest  # noqa: PLC0415

    f = _feature_frame(player_cell, player_timing)
    if len(f) < 100:
        return [], MLValidation(verdict="FAIL", flag_rate=0.0,
                                reasons=["population too small for ML"])

    # per-peer-context z-normalisation (analyze_anomaly.py approach): within
    # (game_id, currency) so scale differences are not "anomalies"
    X = f[FEATURES].astype(float).copy()
    grp = f.groupby(["game_id", "currency"])
    for c in FEATURES:
        mu = grp[c].transform("mean") if c in f else 0
        sd = grp[c].transform("std") if c in f else 1
        X[c] = ((f[c] - mu) / sd.replace(0, np.nan)).fillna(0)

    model = IsolationForest(n_estimators=200, contamination=contamination,
                            random_state=random_state)
    pred = model.fit_predict(X.to_numpy())
    score = -model.score_samples(X.to_numpy())
    f["_flag"] = pred == -1
    f["_score"] = score

    # ---- CLAUDE.md validation gate: segment stability -------------------- #
    overall = float(f._flag.mean())
    reasons: list[str] = []
    seg_rates: dict[str, float] = {}
    f["volume_group"] = pd.qcut(f.rounds, 3, labels=["low", "mid", "high"],
                                duplicates="drop")
    for key in ("game_id", "currency", "sm_tag", "parent", "volume_group"):
        if key not in f:
            continue
        rates = f.groupby(key, observed=True)["_flag"].agg(["mean", "size"])
        rates = rates[rates["size"] >= 50]
        for seg, r in rates.iterrows():
            seg_rates[f"{key}={seg}"] = float(r["mean"])
            if overall > 0 and r["mean"] > 10 * max(overall, contamination):
                reasons.append(
                    f"{key}={seg}: flag rate {r['mean']:.2%} concentrates "
                    f">10x the overall {overall:.2%} -- model is fitting the "
                    f"segment, not anomaly")
    validation = MLValidation(
        verdict="FAIL" if reasons else "PASS",
        flag_rate=overall, segment_rates=seg_rates, reasons=reasons)

    if validation.verdict != "PASS":
        log.warning("ML validation FAILED -- no ML signals emitted: %s",
                    "; ".join(reasons[:3]))
        return [], validation

    # ---- emit ML-DISCOVERY only (independence rule) ----------------------- #
    signals: list[Signal] = []
    for _, row in f[f._flag].iterrows():
        if (row.parent, row.uid) in rule_flagged:
            continue          # correlated-by-construction with Layer 1
        signals.append(Signal(
            signal_id="L3-ML_DISCOVERY", family="ML_DISCOVERY", layer=3,
            grain="player", parent=row.parent, uid=row.uid,
            game_id=None, play_type=None, currency=row.currency, sm_tag=None,
            metric="isolation_score", value=float(row._score),
            threshold=None, threshold_method="ISOLATION_FOREST",
            baseline_source="none", n=int(row.rounds),
            persistence="not_evaluated",
            description=("unusual by unsupervised model on behavioural "
                         "features -- UNUSUAL means unusual, not risky; "
                         "requires investigation to mean anything")))
    log.info("ml discovery: %d signal(s), validation %s",
             len(signals), validation.verdict)
    return signals, validation
