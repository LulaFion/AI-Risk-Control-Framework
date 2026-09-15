"""SQL file loading and safe parameter binding.

SQL lives in riskdet/sql/*.sql -- one file per query, reviewable and versioned,
never assembled by string concatenation in Python code.

Two binding mechanisms, deliberately separate:

  VALUES     -> real BigQuery query parameters (@name). Typed, injection-proof.
                Dates, numbers, strings, arrays.
  IDENTIFIERS-> {{name}} template slots for things query parameters cannot
                express: table names, dataset names. Validated against a strict
                whitelist pattern before substitution -- a table identifier that
                fails validation raises, it is never quoted-and-hoped.

Header contract: each .sql file may carry metadata comment lines that the cost
guard enforces:

    -- measured_bytes: 113159972209
    -- description: one line

measured_bytes is the byte count observed when the query was written. The
client refuses to run a query whose current dry-run estimate exceeds the
recorded measurement by more than a tolerance -- that is the regression brake
that catches an edit turning a 105 GiB query into a 196 GiB one before it
bills, instead of after.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import DataQualityError

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"

# dataset.table or project.dataset.table, standard BigQuery lexical rules.
_IDENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,1023}$")
_SLOT = re.compile(r"\{\{(\w+)\}\}")
_META = re.compile(r"^--\s*(measured_bytes|description|requires_param)\s*:\s*(.+?)\s*$",
                   re.MULTILINE)


@dataclass(frozen=True)
class LoadedSQL:
    name: str
    text: str
    measured_bytes: int | None
    description: str
    # Parameters that MUST be bound or the query refuses to run. Used for the
    # replay boundary: every analytical query over the rounds archive declares
    #   -- requires_param: as_of_time
    # so running it without an event-time bound (i.e. with future-data leakage)
    # is a raised error, not a silent full read.
    required_params: tuple[str, ...] = ()

    def check_params(self, params: dict[str, Any] | None) -> None:
        given = set(params or {})
        missing = [p for p in self.required_params if p not in given]
        if missing:
            raise DataQualityError(
                f"{self.name}: missing required parameter(s) {missing} -- "
                f"as_of_time is the leakage gate and has no default on purpose")


def load(name: str, *, identifiers: dict[str, str] | None = None,
         sql_dir: Path | None = None) -> LoadedSQL:
    """Read riskdet/sql/<name>, substitute identifier slots, parse metadata."""
    base = sql_dir or SQL_DIR
    path = base / name if not name.endswith(".sql") else base / name
    if not path.suffix:
        path = path.with_suffix(".sql")
    if not path.is_file():
        raise DataQualityError(f"SQL file missing: {path}")
    text = path.read_text(encoding="utf-8")

    pairs = _META.findall(text)
    meta = {k: v for k, v in pairs if k != "requires_param"}
    required = tuple(p.strip() for k, v in pairs if k == "requires_param"
                     for p in v.split(","))
    measured = int(meta["measured_bytes"]) if "measured_bytes" in meta else None

    slots = set(_SLOT.findall(text))
    identifiers = identifiers or {}
    missing = slots - identifiers.keys()
    if missing:
        raise DataQualityError(
            f"{path.name}: unbound identifier slot(s): {sorted(missing)}")
    for key, value in identifiers.items():
        if key not in slots:
            continue
        if not _IDENT.match(value):
            raise DataQualityError(
                f"{path.name}: identifier {key}={value!r} fails validation -- "
                f"identifiers are whitelisted, never escaped")
        text = text.replace("{{" + key + "}}", f"`{value}`")

    return LoadedSQL(
        name=path.name,
        text=text,
        measured_bytes=measured,
        description=meta.get("description", ""),
        required_params=required,
    )


def to_query_parameters(params: dict[str, Any] | None) -> list[Any]:
    """Convert a plain dict into typed BigQuery query parameters."""
    if not params:
        return []
    from google.cloud import bigquery  # noqa: PLC0415

    def scalar_type(v: Any) -> str:
        if isinstance(v, bool):
            return "BOOL"
        if isinstance(v, int):
            return "INT64"
        if isinstance(v, float):
            return "FLOAT64"
        if isinstance(v, _dt.datetime):
            return "DATETIME" if v.tzinfo is None else "TIMESTAMP"
        if isinstance(v, _dt.date):
            return "DATE"
        if isinstance(v, str):
            return "STRING"
        raise DataQualityError(f"unsupported query parameter type: {type(v)}")

    out: list[Any] = []
    for name, value in params.items():
        if isinstance(value, (list, tuple, set)):
            vals = list(value)
            if not vals:
                raise DataQualityError(
                    f"parameter @{name} is an empty array -- an empty IN list "
                    f"is almost always a logic error; handle it in the caller")
            out.append(bigquery.ArrayQueryParameter(
                name, scalar_type(vals[0]), vals))
        else:
            out.append(bigquery.ScalarQueryParameter(
                name, scalar_type(value), value))
    return out
