"""PeerKey -- the unit every comparison is made within.

Peer cell = (game_id, play_type, currency, sm_tag).

Why each component is load-bearing:

  game_id    different volatility, hit rate, max multiplier.
  play_type  THE critical one. Certified RTP is keyed on it, stake scale
             differs by up to 300x between playways (a base spin vs a Super
             Free Game buy), and so does variance per unit turnover. Pooling
             playways makes a player who routes turnover into buys look like
             an RTP outlier against a base-game baseline -- a false positive
             manufactured by the key, not the player.
  currency   amounts differ by ~1e5 across MMK/THB/CNY/PHP/USD/VND/MYR, and
             currency proxies the operator population and local timezone.
  sm_tag     a build change legitimately changes payout. Pooling builds lets
             a deploy contaminate its own baseline.

NAMING TRAP, spelled out because three different things wear the same name:

  RecordSlot.game_type   INT64 constant 70  -- product line, useless as a key
  agents' "GameType"     the game identity  -- maps to game_id here
  OMG sheet "GameType"   the playway        -- maps to play_type here

This module therefore never exposes a field called game_type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

# RecordSlot.play_type semantics, verified against the OMG sheet + live data:
PLAY_TYPE_LABELS: dict[int, str] = {
    0: "Base",
    1: "Extra Bet / SureWin",
    2: "Feature Buy (tier 2)",
    3: "Feature Buy (tier 3)",
    4: "Feature Buy (tier 4)",
}
FEATURE_BUY_TYPES: frozenset[int] = frozenset({2, 3, 4})


@dataclass(frozen=True, order=True)
class PeerKey:
    game_id: str
    play_type: int
    currency: str
    sm_tag: str

    def __str__(self) -> str:
        return f"{self.game_id}/pt{self.play_type}/{self.currency}/{self.sm_tag}"

    @property
    def is_feature_buy(self) -> bool:
        return self.play_type in FEATURE_BUY_TYPES

    @property
    def playway_label(self) -> str:
        return PLAY_TYPE_LABELS.get(self.play_type, f"unknown({self.play_type})")

    def widened(self) -> Iterator[tuple[str, tuple]]:
        """Progressively wider groupings, in the order a baseline may fall back
        when a cell is too thin (each step must be REPORTED, never silent):

            drop sm_tag      -> same game/playway/currency, all builds
            drop currency    -> same game/playway (ratios only -- amounts do
                                NOT survive this step)
            drop play_type   -> NEVER offered. That fallback is the pooled-
                                playway error this key exists to prevent.
        """
        yield ("no_sm_tag", (self.game_id, self.play_type, self.currency))
        yield ("no_currency_ratios_only", (self.game_id, self.play_type))


PEER_KEY_COLUMNS: tuple[str, ...] = ("game_id", "play_type", "currency", "sm_tag")
PLAYER_COLUMNS: tuple[str, ...] = ("parent", "uid")
