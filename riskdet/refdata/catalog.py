"""Certified game catalog -- loader for `OMG Game Info.csv`.

This sheet is the absolute reference the whole OUTCOME family compares against:
(GameID, GameType) -> playway, certified RTP, max win multiplier. The sheet's
`GameType` column is RecordSlot's `play_type` (0=Base, 1=Extra Bet/SureWin,
2/3/4=Feature Buy tiers) -- see peer_keys.py for the three-way naming trap.

Known defects of the file, each handled explicitly and reported, never patched
silently:

  encoding     Big5/CP950, NOT mojibake. 0xAD 0xBF decodes to 倍 ("times"), so
               `Feature Buy 100倍` means the buy costs 100x the base bet. A
               UTF-8 read raises; a latin-1 read silently corrupts. Decoding
               chain: cp950 first, then utf-8-sig, then utf-8.
  RTP          percent strings ("96.72%"); missing entirely for some rows
               (1058s, 6801r, 6901g, 1003m).
  GameID       some rows lack the trailing letter the live data has
               ("1013"/"1019" vs live "1013s"/"1019s"). Lookup is
               suffix-tolerant and the anomaly is a WARN, not a guess.
  GameType     blank on some rows -> play_type unknown; the row's RTP applies
               to the game as a whole (source labelled accordingly).
  multi-line   quoted PlayWay cells contain newlines (1028s, 1019, 9001f) --
               csv.reader handles them; naive line-splitting does not.
  non-slots    1002s/1003m are Round games, 6801r/6901g have no metadata.
               Scope exclusion lives in config/exclusions.yaml, not here.

Every certified number this module hands out carries a `source` label so a
report can say WHERE its baseline came from -- CLAUDE.md requires findings be
traceable to source data.
"""

from __future__ import annotations

import csv
import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import DataQualityError

_ENCODINGS = ("cp950", "utf-8-sig", "utf-8")
_RTP_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")
_MULT_RE = re.compile(r"(\d+(?:[\d,]*\d)?(?:\.\d+)?)\s*[xX]")
_BUY_PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*倍")
_GAME_ID_RE = re.compile(r"^(\d+)([a-z]?)$")


@dataclass(frozen=True)
class CatalogRow:
    game_id: str                 # as printed in the sheet, stripped
    play_type: int | None        # sheet "GameType"; None when blank
    game_name: str
    playway: str                 # e.g. "Base", "Feature Buy Re-Spin 35倍"
    rtp: float | None            # fraction: 0.9690, not 96.90
    max_multiplier: float | None # e.g. 5000.0 (from "5000x")
    buy_price_x: float | None    # from 倍 in PlayWay: 35.0, 100.0, 300.0...


@dataclass(frozen=True)
class Issue:
    code: str        # stable, greppable
    severity: str    # WARN | FAIL
    detail: str


@dataclass
class GameCatalog:
    rows: list[CatalogRow]
    issues: list[Issue]
    source_path: Path
    encoding_used: str
    _by_key: dict[tuple[str, int], CatalogRow] = field(default_factory=dict)
    _by_game: dict[str, list[CatalogRow]] = field(default_factory=dict)
    _stem_map: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for r in self.rows:
            self._by_game.setdefault(r.game_id, []).append(r)
            if r.play_type is not None:
                self._by_key[(r.game_id, r.play_type)] = r
            m = _GAME_ID_RE.match(r.game_id)
            if m:
                # stem "1049" -> "1049s"; used for suffix-tolerant lookup
                self._stem_map.setdefault(m.group(1), r.game_id)

    # ------------------------------------------------------------------ #
    def _resolve_game_id(self, game_id: str) -> str | None:
        """Exact match first; then tolerate the sheet's missing-suffix rows
        ("1013" in the sheet vs "1013s" in live data) in both directions."""
        gid = game_id.strip()
        if gid in self._by_game:
            return gid
        m = _GAME_ID_RE.match(gid)
        if m and m.group(1) in self._stem_map:
            return self._stem_map[m.group(1)]
        return None

    def certified_rtp(self, game_id: str, play_type: int | None
                      ) -> tuple[float | None, str]:
        """-> (rtp_fraction | None, source_label).

        source labels, in order of strength:
          certified            exact (game_id, play_type) row
          certified_game_level row matched but its play_type is blank in the
                               sheet -- the number applies to the game, not
                               the playway
          none                 no usable row. Callers must carry
                               baseline_availability='none', not treat as 0.
        """
        gid = self._resolve_game_id(game_id)
        if gid is None:
            return None, "none"
        if play_type is not None:
            row = self._by_key.get((gid, play_type))
            if row is not None and row.rtp is not None:
                return row.rtp, "certified"
        # single game-level row with blank play_type
        rows = self._by_game[gid]
        untyped = [r for r in rows if r.play_type is None and r.rtp is not None]
        if untyped:
            return untyped[0].rtp, "certified_game_level"
        # a base row can stand in for an unknown playway ONLY as game-level info
        if play_type is None:
            base = self._by_key.get((gid, 0))
            if base is not None and base.rtp is not None:
                return base.rtp, "certified_game_level"
        return None, "none"

    def max_multiplier(self, game_id: str, play_type: int | None
                       ) -> tuple[float | None, str]:
        gid = self._resolve_game_id(game_id)
        if gid is None:
            return None, "none"
        if play_type is not None:
            row = self._by_key.get((gid, play_type))
            if row is not None and row.max_multiplier is not None:
                return row.max_multiplier, "certified"
        # max win is usually stated once per game; any row's value applies
        vals = [r.max_multiplier for r in self._by_game[gid]
                if r.max_multiplier is not None]
        if vals:
            return max(vals), "certified_game_level"
        return None, "none"

    def games_without_rtp(self) -> list[str]:
        return sorted({gid for gid, rows in self._by_game.items()
                       if all(r.rtp is None for r in rows)})

    def to_frame(self):
        import pandas as pd  # noqa: PLC0415
        return pd.DataFrame([r.__dict__ for r in self.rows])

    @property
    def verdict(self) -> str:
        if any(i.severity == "FAIL" for i in self.issues):
            return "FAIL"
        return "WARN" if self.issues else "PASS"

    # ------------------------------------------------------------------ #
    def write_dq_report(self, path: Path, run_date: _dt.date | None = None
                        ) -> Path:
        """The data-quality report report-composer greps for 'Verdict:'."""
        run_date = run_date or _dt.date.today()
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# Game catalog data quality -- {run_date.isoformat()}",
            "",
            f"Verdict: {self.verdict}",
            "",
            f"- source: `{self.source_path.name}` (encoding: {self.encoding_used})",
            f"- rows parsed: {len(self.rows)}",
            f"- games: {len(self._by_game)}",
            f"- games with NO certified RTP: "
            f"{', '.join(self.games_without_rtp()) or 'none'}",
            "",
        ]
        if self.issues:
            lines += ["## Issues", "",
                      "| code | severity | detail |", "|---|---|---|"]
            lines += [f"| `{i.code}` | {i.severity} | {i.detail} |"
                      for i in self.issues]
            lines += [
                "",
                "Games without a certified RTP are carried with "
                "`baseline_availability='none'` -- absence of findings for "
                "them means NOT TESTED, not clean.",
            ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


# --------------------------------------------------------------------------- #
def _parse_rtp(raw: str, issues: list[Issue], where: str) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    m = _RTP_RE.match(raw)
    if not m:
        issues.append(Issue("rtp_unparseable", "WARN",
                            f"{where}: RTP {raw!r} not a percent string"))
        return None
    val = float(m.group(1)) / 100.0
    if not 0.5 <= val <= 1.05:
        issues.append(Issue("rtp_out_of_range", "WARN",
                            f"{where}: RTP {val:.4f} outside sanity range"))
        return None
    return val


def load_catalog(path: Path | str,
                 overrides: dict | None = None) -> GameCatalog:
    """Parse the sheet. Never raises for content defects -- they become Issues
    and the verdict; raises only if the file is unreadable or structurally not
    the expected sheet."""
    path = Path(path)
    if not path.is_file():
        raise DataQualityError(f"catalog missing: {path}")

    raw = path.read_bytes()
    text = None
    encoding_used = ""
    for enc in _ENCODINGS:
        try:
            text = raw.decode(enc)
            encoding_used = enc
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise DataQualityError(f"{path.name}: undecodable with {_ENCODINGS}")

    reader = csv.reader(text.splitlines(keepends=True))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise DataQualityError(f"{path.name}: empty file") from exc
    header_norm = [h.strip().lower() for h in header]
    if "gameid" not in header_norm or "rtp" not in header_norm:
        raise DataQualityError(
            f"{path.name}: unexpected header {header!r} -- not the game sheet")

    def col(*names: str) -> int | None:
        for n in names:
            if n in header_norm:
                return header_norm.index(n)
        return None

    i_gid = col("gameid")
    i_pt = col("gametype")
    i_name = col("gamename")
    i_play = col("game playway", "playway")
    # the sheet header is genuinely misspelled: "max mutilipier"
    i_mult = col("max mutilipier", "max multiplier", "max mutiplier")

    i_rtp = header_norm.index("rtp")

    issues: list[Issue] = []
    rows: list[CatalogRow] = []
    seen: set[tuple[str, int | None]] = set()

    for lineno, rec in enumerate(reader, start=2):
        if not rec or not "".join(rec).strip():
            continue
        gid = rec[i_gid].strip() if i_gid is not None and i_gid < len(rec) else ""
        if not gid:
            issues.append(Issue("blank_game_id", "WARN", f"line {lineno}"))
            continue
        where = f"{gid} (line {lineno})"

        m = _GAME_ID_RE.match(gid)
        if not m:
            issues.append(Issue("game_id_unparseable", "WARN",
                                f"{where}: {gid!r}"))
        elif m.group(2) == "":
            issues.append(Issue("game_id_missing_suffix", "WARN",
                                f"{where}: no type letter -- live data uses "
                                f"e.g. {m.group(1)}s; lookup is suffix-tolerant"))

        pt_raw = (rec[i_pt].strip()
                  if i_pt is not None and i_pt < len(rec) else "")
        play_type: int | None = None
        if pt_raw:
            try:
                play_type = int(pt_raw)
            except ValueError:
                issues.append(Issue("play_type_unparseable", "WARN",
                                    f"{where}: GameType {pt_raw!r}"))
        else:
            issues.append(Issue("play_type_blank", "WARN",
                                f"{where}: RTP applies game-level only"))

        key = (gid, play_type)
        if key in seen:
            issues.append(Issue("duplicate_row", "WARN", f"{where}: {key}"))
            continue
        seen.add(key)

        playway = (rec[i_play].strip()
                   if i_play is not None and i_play < len(rec) else "")
        mult_raw = (rec[i_mult].strip()
                    if i_mult is not None and i_mult < len(rec) else "")
        mm = _MULT_RE.search(mult_raw)
        max_mult = float(mm.group(1).replace(",", "")) if mm else None
        if mult_raw and max_mult is None:
            issues.append(Issue("max_multiplier_unparseable", "WARN",
                                f"{where}: {mult_raw!r}"))

        bp = _BUY_PRICE_RE.search(playway)
        buy_price = float(bp.group(1)) if bp else None

        rtp = _parse_rtp(rec[i_rtp] if i_rtp < len(rec) else "", issues, where)
        if rtp is None and not (rec[i_rtp] if i_rtp < len(rec) else "").strip():
            issues.append(Issue("rtp_missing", "WARN",
                                f"{where}: no certified RTP -- "
                                f"baseline_availability will be 'none'"))

        rows.append(CatalogRow(
            game_id=gid, play_type=play_type,
            game_name=(rec[i_name].strip()
                       if i_name is not None and i_name < len(rec) else ""),
            playway=playway, rtp=rtp,
            max_multiplier=max_mult, buy_price_x=buy_price))

    if not rows:
        raise DataQualityError(f"{path.name}: no data rows parsed")

    # manual overrides (config/game_overrides.yaml), applied last and audited
    for key_str, patch in (overrides or {}).items():
        gid, _, pt_s = str(key_str).partition("/")
        pt = int(pt_s) if pt_s not in ("", "None") else None
        for idx, r in enumerate(rows):
            if r.game_id == gid and r.play_type == pt:
                rows[idx] = CatalogRow(**{**r.__dict__, **patch})
                issues.append(Issue("override_applied", "WARN",
                                    f"{gid}/pt{pt}: {patch}"))
                break
        else:
            issues.append(Issue("override_unmatched", "WARN",
                                f"{key_str}: no such row"))

    return GameCatalog(rows=rows, issues=issues, source_path=path,
                       encoding_used=encoding_used)
