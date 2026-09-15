"""Calibration: fit on May, FREEZE, validate on June-July, hide August.

The governing correction (recorded because the first draft got it wrong):
`alert_capacity_ceiling` is a CAPACITY CEILING, not a quota. A quantile tuned
to flag the top 0.1% manufactures candidates from clean data by construction.
Here every rule's boundary is EVIDENCE-BASED first --

    ROBUST metrics   median + N0*MAD of the training population (with the
                     MAD-collapse fallback chain), N0 from config;
    Z metrics        an analytic null per player-cell, Benjamini-Hochberg FDR
                     over the full tested family, PLUS a practical effect
                     floor, PLUS a minimum sample size;

-- and the ceiling only RAISES a boundary when the evidence rule would exceed
alert-handling capacity. Zero findings on clean data is a valid, expected
outcome and validation treats it as PASS.

Baseline hygiene, applied before anything is computed:
  - approved exclusions (config/exclusions.yaml) are dropped;
  - rate baselines are leave-PLAYER-and-OPERATOR-out (23_cell_operator_
    constants), so neither the candidate nor its operator nor a cohort inside
    that operator can set its own reference.

Verdict: fit alone never PASSes. PASS requires frozen parameters to hold on
June-July with sub-ceiling, stable firing rates in EVERY segment -- by game_id,
play_type, currency, sm_tag, operator, and volume group.
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from .config import Settings
from .filters import apply_exclusions
from .refdata import FEATURE_BUY_TYPES, GameCatalog

log = logging.getLogger("riskdet.calibrate")

SEGMENT_KEYS = ("game_id", "play_type", "currency", "sm_tag", "parent",
                "volume_group")


# --------------------------------------------------------------------------- #
# shared statistics
# --------------------------------------------------------------------------- #
def robust_bound(values: np.ndarray, n_mad: float, min_members: int
                 ) -> tuple[float | None, str]:
    """median + n_mad*MAD with the documented MAD-collapse fallback chain.
    Returns (bound, branch). bound=None => metric degenerate, rule disabled."""
    v = values[np.isfinite(values)]
    if v.size < min_members:
        return None, "insufficient_population"
    med = float(np.median(v))
    mad = float(np.median(np.abs(v - med)))
    if mad > 0:
        return med + n_mad * mad, "median+N*MAD"
    nz = v[v > med]
    if nz.size >= min_members:
        nzmed = float(np.median(nz))
        nzmad = float(np.median(np.abs(nz - nzmed)))
        if nzmad > 0:
            return nzmed + n_mad * nzmad, "MAD=0->nonzero median+N*MAD"
        sd = float(nz.std())
        if sd > 0:
            return nzmed + n_mad * sd, "MAD=0->nonzero sd"
    return None, "degenerate_disabled"


def bh_select(pvals: np.ndarray, q: float) -> np.ndarray:
    """Benjamini-Hochberg: boolean mask of selected hypotheses. Ported from the
    sandbox implementation. Empty selection on clean data is the point."""
    m = pvals.size
    if m == 0:
        return np.zeros(0, dtype=bool)
    order = np.argsort(pvals)
    ranked = pvals[order]
    thresh = q * (np.arange(1, m + 1) / m)
    ok = ranked <= thresh
    cut = int(np.max(np.nonzero(ok)[0])) + 1 if ok.any() else 0
    sel = np.zeros(m, dtype=bool)
    sel[order[:cut]] = True
    return sel


def z_to_p_onesided(z: np.ndarray) -> np.ndarray:
    from scipy.stats import norm  # noqa: PLC0415
    return norm.sf(z)


# --------------------------------------------------------------------------- #
# metric construction (shared by fit / validate / Layer 1)
# --------------------------------------------------------------------------- #
@dataclass
class MetricFrame:
    """One evaluated metric over one population window."""
    metric: str
    rule: str
    kind: str                      # ROBUST | Z
    df: pd.DataFrame               # per-entity rows incl. 'value' (+gate cols)
    gates_note: str = ""


def _volume_group(rounds: pd.Series) -> pd.Series:
    try:
        return pd.qcut(rounds, 3, labels=["low", "mid", "high"], duplicates="drop")
    except ValueError:
        return pd.Series("all", index=rounds.index)


def build_metrics(cfg: Settings, catalog: GameCatalog,
                  player_cell: pd.DataFrame, player_timing: pd.DataFrame,
                  cell_constants: pd.DataFrame,
                  cell_operator: pd.DataFrame) -> list[MetricFrame]:
    rules = cfg.thresholds["rules"]
    out: list[MetricFrame] = []

    pc, _ = apply_exclusions(player_cell, cfg)
    pt, _ = apply_exclusions(player_timing, cfg)
    co, _ = apply_exclusions(cell_operator, cfg)
    cc = cell_constants  # cell grain -- exclusions apply via co corrections

    cell_keys = ["game_id", "play_type", "currency", "sm_tag"]

    # ---------- ROBUST: routing + timing ------------------------------- #
    per_player = pc.groupby(["parent", "uid"], as_index=False).agg(
        turnover=("turnover", "sum"), rounds=("rounds", "sum"))
    buys = (pc[pc.play_type.isin(FEATURE_BUY_TYPES)]
            .groupby(["parent", "uid"], as_index=False)
            .agg(buy_turnover=("turnover", "sum")))
    ppl = per_player.merge(buys, on=["parent", "uid"], how="left")
    ppl["buy_turnover"] = ppl.buy_turnover.fillna(0.0)
    ppl["value"] = ppl.buy_turnover / ppl.turnover.replace(0, np.nan)
    ppl["volume_group"] = _volume_group(ppl.rounds)
    out.append(MetricFrame("buy_share_turnover", "L1-FEATURE_ROUTING",
                           "ROBUST", ppl.dropna(subset=["value"])))

    t = pt.copy()
    t["volume_group"] = _volume_group(t.rounds)
    mg = int(rules["L1-SAME_SECOND"].get("min_gaps", 500))
    ss = t[t.n_gaps >= mg].copy()
    ss["value"] = ss.same_second_gaps / ss.n_gaps
    out.append(MetricFrame("same_second_share", "L1-SAME_SECOND", "ROBUST", ss))

    mir = int(rules["L1-CADENCE_MODE"].get("min_in_range_gaps", 1000))
    cm = t[t.in_range_gaps >= mir].copy()
    cm["value"] = cm.modal_share
    out.append(MetricFrame("modal_share", "L1-CADENCE_MODE", "ROBUST", cm))

    cr = t.copy()
    cr["value"] = cr.concurrent_rounds.astype(float)
    out.append(MetricFrame("concurrent_rounds", "L1-CONCURRENT", "ROBUST", cr))

    rc = t.copy()
    rc["value"] = rc.max_rounds_per_minute.astype(float)
    out.append(MetricFrame("max_rounds_per_minute", "L1-RATE_CEILING",
                           "ROBUST", rc))

    # ---------- Z: leave-player-AND-operator-out rate tests -------------- #
    co_idx = co.set_index(cell_keys + ["parent"])

    def loo_rate(base: pd.DataFrame, num_col: str, cell_num_rate: str
                 ) -> tuple[pd.Series, pd.Series]:
        """p0 excluding the player's own rounds AND their operator's rounds."""
        cellstats = cc.set_index(cell_keys)
        joined = base.join(cellstats[["rounds", cell_num_rate]],
                           on=cell_keys, rsuffix="_cell")
        op = co_idx.reindex(
            pd.MultiIndex.from_frame(base[cell_keys + ["parent"]]))
        op_rounds = pd.Series(op["rounds"].to_numpy(), index=base.index).fillna(0)
        op_num = pd.Series(op[num_col].to_numpy(), index=base.index).fillna(0)
        cell_rounds = joined["rounds_cell"]
        cell_num = joined[cell_num_rate] * cell_rounds
        denom = (cell_rounds - op_rounds).clip(lower=0)
        num = (cell_num - op_num).clip(lower=0)
        p0 = (num / denom.replace(0, np.nan)).clip(1e-9, 1 - 1e-9)
        return p0, denom

    base0 = pc[pc.play_type == 0].copy()
    base0["volume_group"] = _volume_group(base0.rounds)

    for metric, rule_name, num_col, rate_col, own_col in (
            ("trigger_rate_z", "L1-TRIGGER_RATE", "n_trigger", "p_trigger",
             "n_trigger"),
            ("hit_rate_z", "L1-HIT_RATE", "n_win", "p_hit", "n_win")):
        rcfg = rules[rule_name]
        b = base0[base0.rounds >= int(rcfg.get("min_rounds", 2000))].copy()
        if b.empty:
            out.append(MetricFrame(metric, rule_name, "Z",
                                   b.assign(value=np.nan, lift=np.nan)))
            continue
        p0, peer_rounds = loo_rate(b, num_col, rate_col)
        phat = b[own_col] / b.rounds
        b["value"] = ((phat - p0)
                      / np.sqrt(p0 * (1 - p0) / b.rounds))
        b["lift"] = phat / p0
        b["p0_frozen"] = p0          # carried for half-window persistence
        b["peer_rounds"] = peer_rounds
        # peer must still be a real population after removing player+operator
        b = b[b.peer_rounds >= int(cfg.thresholds["peer"].get("min_players", 30))
              * int(cfg.thresholds["global"]["min_rounds_per_cell"])]
        out.append(MetricFrame(metric, rule_name, "Z",
                               b.dropna(subset=["value"])))

    # ---------- Z: excess money vs certified RTP ------------------------- #
    mm = pc.merge(cc[cell_keys + ["sigma_spin"]], on=cell_keys, how="left")
    mm["rtp_cert"] = mm.apply(
        lambda r: (catalog.certified_rtp(r.game_id, int(r.play_type))[0]),
        axis=1)
    mm = mm.dropna(subset=["rtp_cert", "sigma_spin"])
    mm = mm[(mm.sigma_spin > 0) & (mm.sumsq_valid_bet > 0)].copy()
    mm["excess"] = mm.total_win - mm.rtp_cert * mm.turnover
    # CORRECT SE for unequal stakes: Var = sumsq_valid_bet * sigma_spin^2.
    # (mean_bet * sqrt(n) understates whenever stakes vary -- buys are
    # 32.5-300x base -- and understating the SE manufactures significance.)
    mm["value"] = mm.excess / (mm.sigma_spin * np.sqrt(mm.sumsq_valid_bet))
    mm["effect_rtp_points"] = 100.0 * mm.excess / mm.turnover.replace(0, np.nan)
    mm["volume_group"] = _volume_group(mm.rounds)
    out.append(MetricFrame("excess_turnover_z", "L1-EXCESS_TURNOVER", "Z",
                           mm.dropna(subset=["value"])))

    return out


# --------------------------------------------------------------------------- #
# fit -> FrozenParams
# --------------------------------------------------------------------------- #
@dataclass
class FrozenMetric:
    metric: str
    rule: str
    kind: str
    enabled: bool
    branch: str
    # ROBUST
    bound: float | None = None
    ceiling_bound: float | None = None
    # Z
    fdr_q: float | None = None
    min_lift: float | None = None
    min_effect_rtp_points: float | None = None
    train_eligible: int = 0
    train_fire_rate: float = 0.0
    note: str = ""


@dataclass
class FrozenParams:
    train_as_of: _dt.datetime
    ceiling: float
    metrics: dict[str, FrozenMetric] = field(default_factory=dict)


def fit(cfg: Settings, metrics: list[MetricFrame],
        train_as_of: _dt.datetime) -> FrozenParams:
    cal = cfg.thresholds["calibration"]
    g = cfg.thresholds["global"]
    ceiling = float(cal.get("alert_capacity_ceiling", 0.001))
    n0 = float(g.get("mad_multiplier_initial", 12))
    min_members = int(g.get("mad_collapse_min_members", 20))
    rules = cfg.thresholds["rules"]

    frozen = FrozenParams(train_as_of=train_as_of, ceiling=ceiling)
    for mf in metrics:
        vals = mf.df["value"].to_numpy(dtype=float) if len(mf.df) else np.array([])
        if mf.kind == "ROBUST":
            bound, branch = robust_bound(vals, n0, min_members)
            ceil_bound = (float(np.quantile(vals[np.isfinite(vals)],
                                            1 - ceiling))
                          if vals.size else None)
            final = bound
            note = ""
            if bound is not None and ceil_bound is not None and ceil_bound > bound:
                # capacity may only RAISE the bar, never lower it
                final = ceil_bound
                note = (f"evidence bound {bound:.5g} would exceed capacity; "
                        f"raised to ceiling quantile {ceil_bound:.5g}")
            rate = float(np.mean(vals > final)) if (final is not None
                                                    and vals.size) else 0.0
            frozen.metrics[mf.metric] = FrozenMetric(
                metric=mf.metric, rule=mf.rule, kind="ROBUST",
                enabled=final is not None, branch=branch,
                bound=final, ceiling_bound=ceil_bound,
                train_eligible=int(vals.size), train_fire_rate=rate, note=note)
        else:  # Z
            rcfg = rules.get(mf.rule, {})
            q = float(rcfg.get("fdr_q", 0.01))
            fm = FrozenMetric(
                metric=mf.metric, rule=mf.rule, kind="Z", enabled=True,
                branch="analytic_null+BH_FDR", fdr_q=q,
                min_lift=float(rcfg.get("min_lift", 2.0))
                         if "rate" in mf.metric else None,
                min_effect_rtp_points=float(
                    rcfg.get("effect_floor_rtp_points_initial", 3.0))
                    if mf.metric == "excess_turnover_z" else None,
                train_eligible=int(len(mf.df)))
            fired = evaluate_z(mf.df, fm)
            fm.train_fire_rate = (float(fired.mean()) if len(fired) else 0.0)
            frozen.metrics[mf.metric] = fm
    return frozen


def evaluate_z(df: pd.DataFrame, fm: FrozenMetric) -> pd.Series:
    """Candidate mask for a Z metric: BH-FDR over the FULL tested family, plus
    effect + sample gates. Empty selection on clean data is expected."""
    if df.empty:
        return pd.Series(dtype=bool)
    z = df["value"].to_numpy(dtype=float)
    sel = bh_select(z_to_p_onesided(z), fm.fdr_q or 0.01)
    mask = pd.Series(sel, index=df.index)
    if fm.min_lift is not None and "lift" in df:
        mask &= df["lift"] >= fm.min_lift
    if fm.min_effect_rtp_points is not None and "effect_rtp_points" in df:
        mask &= df["effect_rtp_points"] >= fm.min_effect_rtp_points
    return mask


def evaluate(df: pd.DataFrame, fm: FrozenMetric) -> pd.Series:
    if not fm.enabled or df.empty:
        return pd.Series(False, index=df.index)
    if fm.kind == "ROBUST":
        return df["value"] > (fm.bound if fm.bound is not None else np.inf)
    return evaluate_z(df, fm)


# --------------------------------------------------------------------------- #
# validate on June-July with FROZEN params
# --------------------------------------------------------------------------- #
@dataclass
class SegmentRate:
    metric: str
    segment_key: str
    segment_value: str
    eligible: int
    fired: int

    @property
    def rate(self) -> float:
        return self.fired / self.eligible if self.eligible else 0.0


@dataclass
class ValidationResult:
    as_of: _dt.datetime
    overall: dict[str, tuple[int, int]] = field(default_factory=dict)
    segments: list[SegmentRate] = field(default_factory=list)
    verdict: str = "FAIL"
    reasons: list[str] = field(default_factory=list)


def validate(cfg: Settings, frozen: FrozenParams,
             metrics: list[MetricFrame],
             as_of: _dt.datetime) -> ValidationResult:
    cal = cfg.thresholds["calibration"]
    ceiling = frozen.ceiling
    seg_factor = float(cal.get("stability_segment_max_factor", 10))
    abort = float(cal.get("saturation_abort_rate", 0.05))
    min_seg = 50  # a rate over fewer entities than this is noise, not evidence

    res = ValidationResult(as_of=as_of)
    worst: list[str] = []
    for mf in metrics:
        fm = frozen.metrics.get(mf.metric)
        if fm is None or not fm.enabled:
            continue
        fired = evaluate(mf.df, fm)
        n, k = int(len(mf.df)), int(fired.sum())
        res.overall[mf.metric] = (n, k)
        rate = k / n if n else 0.0
        if rate > abort:
            worst.append(f"{mf.metric}: overall rate {rate:.3%} > saturation "
                         f"abort {abort:.1%}")
        elif rate > ceiling:
            worst.append(f"{mf.metric}: overall rate {rate:.3%} > capacity "
                         f"ceiling {ceiling:.3%}")
        for key in SEGMENT_KEYS:
            if key not in mf.df.columns:
                continue
            grp = mf.df.assign(_f=fired).groupby(key, observed=True)["_f"]
            for seg, s in grp.agg(["size", "sum"]).iterrows():
                sr = SegmentRate(mf.metric, key, str(seg),
                                 int(s["size"]), int(s["sum"]))
                res.segments.append(sr)
                if sr.eligible >= min_seg and sr.rate > max(
                        ceiling * seg_factor, 1.0 / sr.eligible):
                    worst.append(
                        f"{mf.metric}: segment {key}={seg} rate "
                        f"{sr.rate:.3%} over {sr.eligible:,} entities exceeds "
                        f"{ceiling:.3%} x {seg_factor:g}")
    res.reasons = worst
    # Zero findings everywhere is an explicit PASS: clean data is allowed to
    # be clean. FAIL only on saturation; WARN on ceiling/stability breaches.
    if any("saturation" in w for w in worst):
        res.verdict = "FAIL"
    elif worst:
        res.verdict = "WARN"
    else:
        res.verdict = "PASS"
    return res


def load_frozen(cfg: Settings) -> FrozenParams:
    """Reload the frozen parameters a calibration run persisted. Refuses to
    invent defaults: a scan without a calibration artifact is a scan with
    guessed thresholds, which is the failure mode this design retired."""
    import yaml  # noqa: PLC0415
    from .errors import DataQualityError  # noqa: PLC0415
    path = cfg.paths.out / "calibrated_thresholds.yaml"
    if not path.is_file():
        raise DataQualityError(
            f"{path} missing -- run `python -m riskdet calibrate` first. "
            f"Scanning without frozen calibrated parameters is not supported.")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    frozen = FrozenParams(
        train_as_of=_dt.datetime.fromisoformat(doc["train_as_of"]),
        ceiling=float(doc["alert_capacity_ceiling"]))
    for name, m in (doc.get("metrics") or {}).items():
        frozen.metrics[name] = FrozenMetric(**m)
    return frozen


# --------------------------------------------------------------------------- #
# artifacts
# --------------------------------------------------------------------------- #
def write_artifacts(cfg: Settings, frozen: FrozenParams,
                    val: ValidationResult) -> tuple[Path, Path]:
    import yaml  # noqa: PLC0415
    cfg.paths.ensure()
    out_yaml = cfg.paths.out / "calibrated_thresholds.yaml"
    out_yaml.write_text(yaml.safe_dump({
        "calibrated_at_utc": _dt.datetime.now(_dt.timezone.utc)
                             .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "train_as_of": frozen.train_as_of.isoformat(),
        "validated_as_of": val.as_of.isoformat(),
        "validation_verdict": val.verdict,
        "alert_capacity_ceiling": frozen.ceiling,
        "metrics": {k: vars(v) for k, v in frozen.metrics.items()},
    }, sort_keys=False), encoding="utf-8")

    run_date = _dt.date.today()
    report = cfg.paths.dq_report("calibration", run_date)
    lines = [
        f"# Calibration -- fit May, frozen, validated June-July -- "
        f"{run_date.isoformat()}", "",
        f"Verdict: {val.verdict}", "",
        f"- train boundary (UTC event time): `{frozen.train_as_of.isoformat()}`",
        f"- validation boundary: `{val.as_of.isoformat()}`",
        f"- alert capacity ceiling: {frozen.ceiling:.3%} "
        f"(a CEILING -- zero findings on clean data is a PASS)", "",
        "| metric | rule | kind | enabled | branch | bound | train n | "
        "train rate | val n | val fired |",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for m in frozen.metrics.values():
        vn, vk = val.overall.get(m.metric, (0, 0))
        bound = "-" if m.bound is None else f"{m.bound:.5g}"
        lines.append(
            f"| `{m.metric}` | {m.rule} | {m.kind} | {m.enabled} | {m.branch} "
            f"| {bound} | {m.train_eligible:,} | {m.train_fire_rate:.4%} "
            f"| {vn:,} | {vk} |")
    if val.reasons:
        lines += ["", "## Stability breaches", ""]
        lines += [f"- {r}" for r in val.reasons]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_yaml, report
