"""Artifact paths and case ids -- the single source of truth.

Two directories, deliberately separate (see CLAUDE.md Layer 4):

  out/     machine data: CSV, JSONL, JSON. Intermediate, reproducible.
  output/  the CONTRACT SURFACE the Claude Code subagents read. Markdown plus
           the two CSVs they name. `report-composer` iterates output/cases/ and
           skips non-case files, so intermediates must never land there.

No other module builds a path by string concatenation. If a path is needed,
it is defined here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# parent/uid/game_id arrive from an external system as free-form strings. A '/'
# or '..' in a uid is a path-traversal write, so every component is sanitised
# before it reaches the filesystem.
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")
_MAX_COMPONENT = 64


def sanitize(component: str | int | None, *, fallback: str = "NA") -> str:
    """Reduce an arbitrary identifier to a filesystem-safe token."""
    if component is None:
        return fallback
    s = _SAFE.sub("_", str(component)).strip("._-")
    if not s:
        return fallback
    return s[:_MAX_COMPONENT]


def player_case_id(parent: str, game_id: str | None, uid: str) -> str:
    """Case id for a player finding.

    Mirrors the subagents' (WebSite, GameType, UserId) triple, which maps 1:1
    onto this schema: WebSite->parent, GameType->game_id, UserId->uid.
    A player-wide case (spanning games) uses 'ALL' in the game slot.
    """
    return (f"P_{sanitize(parent)}_{sanitize(game_id, fallback='ALL')}"
            f"_{sanitize(uid)}")


def game_case_id(game_id: str, play_type: int | None, sm_tag: str | None,
                 report_date: date | str) -> str:
    """Case id for a game-level finding. Includes play_type because certified
    RTP is keyed on it -- a base game and its 300x feature buy are different
    products and must not share a case."""
    return (f"G_{sanitize(game_id)}_pt{sanitize(play_type, fallback='NA')}"
            f"_{sanitize(sm_tag, fallback='NOTAG')}_{sanitize(report_date)}")


@dataclass(frozen=True)
class Paths:
    """Resolved artifact locations for one run."""

    root: Path
    out: Path
    artifacts: Path

    # ---- out/ : machine data -------------------------------------------
    @property
    def candidates_jsonl(self) -> Path:
        return self.out / "candidates.jsonl"

    @property
    def player_features(self) -> Path:
        return self.out / "player_cell_features.parquet"

    @property
    def cell_constants(self) -> Path:
        return self.out / "cell_constants.parquet"

    @property
    def quantiles(self) -> Path:
        return self.out / "cell_quantiles.parquet"

    @property
    def logs_dir(self) -> Path:
        return self.out / "logs"

    def log_evidence(self, case_id: str, game_seq_id: str) -> Path:
        return self.logs_dir / sanitize(case_id) / f"{sanitize(game_seq_id)}.json"

    @property
    def log_index(self) -> Path:
        return self.logs_dir / "_index.csv"

    # ---- output/ : Layer 4 contract surface ----------------------------
    @property
    def case_queue(self) -> Path:
        """Read by player-investigator and report-composer."""
        return self.artifacts / "case_queue.md"

    @property
    def findings_csv(self) -> Path:
        """report-composer names this file and checks it for staleness, so it
        must carry window_start/window_end columns."""
        return self.artifacts / "risk_rules_findings.csv"

    @property
    def cases_dir(self) -> Path:
        """Python creates the directory; the agents write the files."""
        return self.artifacts / "cases"

    def case_file(self, case_id: str) -> Path:
        return self.cases_dir / f"{sanitize(case_id)}.md"

    @property
    def reports_dir(self) -> Path:
        return self.artifacts / "reports"

    def daily_digest(self, start: date | str, end: date | str) -> Path:
        return self.reports_dir / f"daily_digest_{sanitize(start)}_{sanitize(end)}.md"

    @property
    def dq_dir(self) -> Path:
        """report-composer puts any WARN/FAIL here at the TOP of the digest.
        Every .md in here must contain a literal 'Verdict:' line."""
        return self.artifacts / "data_quality"

    def dq_report(self, name: str, run_date: date | str) -> Path:
        return self.dq_dir / f"{sanitize(name)}_{sanitize(run_date)}.md"

    @property
    def cost_ledger(self) -> Path:
        return self.dq_dir / "query_cost_ledger.csv"

    # ---- lifecycle -----------------------------------------------------
    def ensure(self) -> "Paths":
        """Create every directory the pipeline and the agents rely on."""
        for d in (self.out, self.logs_dir, self.artifacts,
                  self.cases_dir, self.reports_dir, self.dq_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self


def resolve(root: Path, out_dir: Path | None = None,
            artifact_dir: Path | None = None) -> Paths:
    root = Path(root)
    return Paths(
        root=root,
        out=Path(out_dir) if out_dir else root / "out",
        artifacts=Path(artifact_dir) if artifact_dir else root / "output",
    )
