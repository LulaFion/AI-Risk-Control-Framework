"""Population hygiene: the approved exclusion list and quarantine marking.

Applied BEFORE any baseline or threshold is computed, so excluded/quarantined
populations can never contaminate what "normal" means. CLAUDE.md is strict
here: exclusion happens ONLY from the approved list (config/exclusions.yaml);
structurally-detected fleets are quarantined -- kept visible, kept out of
baselines, never escalated -- until a human moves them onto the list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

from .config import Settings


@dataclass(frozen=True)
class ExclusionReport:
    rows_in: int
    rows_out: int
    dropped_fleet_rows: int
    dropped_account_rows: int
    dropped_scope_parent_rows: int
    dropped_scope_game_rows: int

    @property
    def dropped(self) -> int:
        return self.rows_in - self.rows_out


def apply_exclusions(df: pd.DataFrame, cfg: Settings) -> tuple[pd.DataFrame, ExclusionReport]:
    """Drop rows covered by the APPROVED exclusion list. Anything structural
    (proposed fleets) is NOT dropped here -- quarantine is a downstream state,
    not a filter, so the skeptic can still see those accounts."""
    x = cfg.exclusions or {}
    n_in = len(df)
    mask = pd.Series(True, index=df.index)
    d_fleet = d_acct = d_par = d_game = 0

    for entry in x.get("approved_fleets") or []:
        m = pd.Series(True, index=df.index)
        if entry.get("parent") is not None and "parent" in df:
            m &= df["parent"].astype(str) == str(entry["parent"])
        pat = entry.get("uid_pattern")
        if pat and "uid" in df:
            m &= df["uid"].astype(str).str.match(re.compile(pat))
        d_fleet += int((mask & m).sum())
        mask &= ~m

    for entry in x.get("approved_accounts") or []:
        if "parent" in df and "uid" in df:
            m = ((df["parent"].astype(str) == str(entry.get("parent")))
                 & (df["uid"].astype(str) == str(entry.get("uid"))))
            d_acct += int((mask & m).sum())
            mask &= ~m

    oos_parents = {str(p) for p in (x.get("out_of_scope_parents") or [])}
    if oos_parents and "parent" in df:
        m = df["parent"].astype(str).isin(oos_parents)
        d_par += int((mask & m).sum())
        mask &= ~m

    oos_games = {str(g["game_id"]) for g in (x.get("out_of_scope_games") or [])}
    if oos_games and "game_id" in df:
        m = df["game_id"].astype(str).isin(oos_games)
        d_game += int((mask & m).sum())
        mask &= ~m

    out = df[mask]
    return out, ExclusionReport(
        rows_in=n_in, rows_out=len(out),
        dropped_fleet_rows=d_fleet, dropped_account_rows=d_acct,
        dropped_scope_parent_rows=d_par, dropped_scope_game_rows=d_game)
