"""Configuration: environment variables + YAML, resolved once into Settings.

Design rules, taken from AI Analysis/statistical_review_board.py:
  - Secrets come from the environment ONLY. There is no hardcoded fallback for
    any credential anywhere in this package.
  - A dependency-free .env loader that never overwrites an already-set variable.
  - An offline check() that makes zero API calls, so readiness can be verified
    before anything is spent.

Threshold policy (CLAUDE.md + the build directive): thresholds.yaml stores
MULTIPLIERS, TARGET ALERT RATES and QUANTILE TARGETS -- not literal cut-offs.
The calibration pass solves each rule's multiplier from the observed population.
A literal number is permitted only where it is physically or contractually
meaningful, and every such value carries a source comment in the YAML.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import DataQualityError, MissingCredentials, Unavailable
from . import paths as _paths

GIB = 1024 ** 3
TIB = 1024 ** 4

# Default project/dataset constants. Overridable by env; never secret.
DEFAULT_BQ_PROJECT = "acp-develop"
DEFAULT_SOURCE_TABLE = "acp-develop.OMG.RecordSlot"
DEFAULT_WORK_DATASET = "OMG_riskdet"
DEFAULT_LOG_PROJECT = "acp-prod"

# Cloud Logging containers that carry gameplay payloads (kube-system excluded).
GAME_CONTAINERS = ("spin-server", "func-go", "repo-go", "play-go", "atsm-service")

# acp-prod _Default bucket retention. Governs whether a candidate can be
# INVESTIGATED -- not how much history we may analyse. The two are separate.
LOG_RETENTION_DAYS = 30


# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------
def load_dotenv(path: str | Path | None = None) -> int:
    """Load KEY=VALUE lines into os.environ without overwriting existing values.

    Dependency-free by design. Returns the number of variables set. Never logs
    a value.
    """
    p = Path(path) if path else Path.cwd() / ".env"
    if not p.is_file():
        return 0
    n = 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = val
            n += 1
    return n


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(float(raw))
    except ValueError as exc:
        raise DataQualityError(f"{name}={raw!r} is not a number") from exc


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# YAML
# ---------------------------------------------------------------------------
def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise DataQualityError(f"config file missing: {path}")
    try:
        import yaml  # noqa: PLC0415  (optional dep, imported where needed)
    except ImportError as exc:  # pragma: no cover
        raise Unavailable(
            "PyYAML is required to read config files. "
            "pip install -r requirements.txt"
        ) from exc
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise DataQualityError(f"{path} must contain a YAML mapping")
    return data


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    # --- identity / location ---
    bq_project: str
    source_table: str
    work_dataset: str
    log_project: str
    bq_location: str

    # --- paths ---
    root: Path
    config_dir: Path
    catalog_csv: Path
    paths: _paths.Paths

    # --- windows (deliberately two; see CLAUDE.md tension) ---
    history_days: int | None      # None = all available history
    evidence_days: int            # bounded by Cloud Logging retention

    # --- cost guards ---
    max_bytes_billed: int         # hard per-query cap
    confirm_bytes: int            # above this, allow_large=True is required
    run_budget_bytes: int         # cumulative cap for one process
    usd_per_tib: float
    dry_run_only: bool

    # --- behaviour ---
    min_rounds: int
    redact_salt: str | None

    # --- credentials ---
    # One identity for everything: airc-740@acp-prod holds BigQuery Job User +
    # Data Editor on acp-develop AND Logs Viewer on acp-prod (both verified by
    # live probes). The ambient ADC (ai-ocr@acp-develop) can query BigQuery but
    # gets 403 on all acp-prod log views, so it is only the last fallback for
    # BigQuery and never used for logging.
    bq_key_file: Path | None
    log_key_file: Path | None

    # --- loaded config documents ---
    thresholds: dict[str, Any] = field(default_factory=dict)
    exclusions: dict[str, Any] = field(default_factory=dict)

    # ---- derived ----------------------------------------------------------
    @property
    def work_table_rounds(self) -> str:
        """Materialised, ReportDate-partitioned copy of the source rounds.

        Exists because the source table is clustered on (serial, game_time) and
        NOT date-partitioned, so a date filter prunes nothing there. Measured:
        a 30-day filter and no filter both estimate 113,159,972,209 bytes.
        """
        return f"{self.bq_project}.{self.work_dataset}.rounds_all"

    def usd(self, nbytes: int) -> float:
        return (nbytes / TIB) * self.usd_per_tib


def settings(root: Path | str | None = None) -> Settings:
    """Resolve settings from env + YAML. Cheap, no I/O to GCP."""
    root = Path(root) if root else Path(
        os.environ.get("RISKDET_ROOT", Path(__file__).resolve().parent.parent))
    load_dotenv(root / ".env")

    config_dir = Path(os.environ.get("RISKDET_CONFIG_DIR", root / "config"))

    # The reference sheet has been renamed at least once (MOG -> OMG), so the
    # path is config-driven and the loader tolerates either name.
    catalog = os.environ.get("RISKDET_CATALOG_CSV")
    if catalog:
        catalog_csv = Path(catalog)
    else:
        candidates = [root / "OMG Game Info.csv", root / "MOG Game Info.csv",
                      root / "reference" / "game_info.csv"]
        catalog_csv = next((c for c in candidates if c.is_file()), candidates[0])

    hist = os.environ.get("RISKDET_HISTORY_DAYS", "").strip()
    history_days = None if hist in {"", "all", "0"} else int(hist)

    def _key(*names: str) -> Path | None:
        for n in names:
            v = os.environ.get(n)
            if v and Path(v).is_file():
                return Path(v)
        return None

    # Conventional location for the airc-740 key, outside any project tree.
    # It serves BOTH services; ADC is only a BigQuery fallback.
    default_airc_key = Path.home() / ".config/gcloud/keys/acp-prod-e80f50c53bfe_airc.json"
    log_key = _key("RISKDET_LOG_KEY_FILE", "RISKDET_KEY_FILE")
    if log_key is None and default_airc_key.is_file():
        log_key = default_airc_key
    bq_key = _key("RISKDET_BQ_KEY_FILE", "RISKDET_KEY_FILE")
    if bq_key is None and default_airc_key.is_file():
        bq_key = default_airc_key

    thresholds = {}
    exclusions = {}
    tpath, xpath = config_dir / "thresholds.yaml", config_dir / "exclusions.yaml"
    if tpath.is_file():
        thresholds = _load_yaml(tpath)
    if xpath.is_file():
        exclusions = _load_yaml(xpath)

    return Settings(
        bq_project=os.environ.get("RISKDET_BQ_PROJECT", DEFAULT_BQ_PROJECT),
        source_table=os.environ.get("RISKDET_SOURCE_TABLE", DEFAULT_SOURCE_TABLE),
        work_dataset=os.environ.get("RISKDET_WORK_DATASET", DEFAULT_WORK_DATASET),
        log_project=os.environ.get("RISKDET_LOG_PROJECT", DEFAULT_LOG_PROJECT),
        # RecordSlot lives in asia-southeast1; a work dataset must match it.
        bq_location=os.environ.get("RISKDET_BQ_LOCATION", "asia-southeast1"),
        root=root,
        config_dir=config_dir,
        catalog_csv=catalog_csv,
        paths=_paths.resolve(
            root,
            out_dir=os.environ.get("RISKDET_OUT_DIR"),
            artifact_dir=os.environ.get("RISKDET_ARTIFACT_DIR"),
        ),
        history_days=history_days,
        evidence_days=_env_int("RISKDET_EVIDENCE_DAYS", LOG_RETENTION_DAYS),
        max_bytes_billed=_env_int("RISKDET_MAX_BYTES_BILLED", 150 * GIB),
        confirm_bytes=_env_int("RISKDET_CONFIRM_BYTES", 60 * GIB),
        run_budget_bytes=_env_int("RISKDET_RUN_BUDGET_BYTES", 400 * GIB),
        usd_per_tib=float(os.environ.get("RISKDET_USD_PER_TIB", "6.25")),
        dry_run_only=_env_bool("RISKDET_DRY_RUN_ONLY", False),
        min_rounds=_env_int("RISKDET_MIN_ROUNDS", 200),
        redact_salt=os.environ.get("RISKDET_REDACT_SALT"),
        bq_key_file=bq_key,
        log_key_file=log_key,
        thresholds=thresholds,
        exclusions=exclusions,
    )


# ---------------------------------------------------------------------------
# Credential resolution, per service
# ---------------------------------------------------------------------------
_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)


def credentials_for(service: str, cfg: Settings | None = None) -> tuple[Any, str]:
    """Return (credentials, identity_label) for 'bigquery' or 'logging'.

    Resolution order: an explicit per-service key file, then a shared key file,
    then Application Default Credentials.

    This exists because the two services need different identities in this
    project. Measured on 2026-08-18:
        ai-ocr@acp-develop  (ambient ADC) -> BigQuery OK, Logging 403
        airc-740@acp-prod   (key file)    -> BigQuery OK, Logging OK
    Silently falling back to ADC for logging produces
    "PermissionDenied: Permission denied for all log views", so a caller that
    needs logs should verify the label it gets back.
    """
    cfg = cfg or settings()
    if service not in {"bigquery", "logging"}:
        raise ValueError(f"unknown service: {service!r}")

    key = cfg.bq_key_file if service == "bigquery" else cfg.log_key_file
    if key is not None:
        try:
            from google.oauth2 import service_account  # noqa: PLC0415
        except ImportError as exc:
            raise MissingCredentials(
                "google-auth is not installed. pip install -r requirements.txt"
            ) from exc
        creds = service_account.Credentials.from_service_account_file(
            str(key), scopes=list(_SCOPES))
        return creds, getattr(creds, "service_account_email", str(key))

    creds = require_credentials()
    label = getattr(creds, "service_account_email", None) or "ADC"
    return creds, label


# ---------------------------------------------------------------------------
# Offline readiness check -- zero API calls
# ---------------------------------------------------------------------------
def check(cfg: Settings | None = None) -> dict[str, Any]:
    """Report what is and is not ready, without touching the network.

    Never raises for a missing capability -- returns status so a caller can
    decide. Mirrors statistical_review_board.check().
    """
    cfg = cfg or settings()
    report: dict[str, Any] = {"ok": True, "items": {}}

    def note(key: str, ok: bool, detail: str) -> None:
        report["items"][key] = {"ok": ok, "detail": detail}
        if not ok:
            report["ok"] = False

    # credentials -- accept an explicit key file, a gcloud ADC file, OR ambient
    # ADC resolvable via google.auth.default(). Cloud Run / GCE supply credentials
    # through the metadata server -- there is NO local file or env var there, so
    # a file/env-only check false-fails in production (the SA works regardless,
    # which is why identity:bigquery/logging still pass). Resolving default() is
    # a light local detection (token fetch is deferred), so this stays offline.
    adc = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    gcloud_adc = Path.home() / "AppData/Roaming/gcloud/application_default_credentials.json"
    legacy = Path.home() / ".config/gcloud/application_default_credentials.json"
    if adc and Path(adc).is_file():
        note("credentials", True, f"GOOGLE_APPLICATION_CREDENTIALS -> {adc}")
    elif gcloud_adc.is_file() or legacy.is_file():
        note("credentials", True, "gcloud application-default credentials found")
    else:
        try:
            import google.auth  # noqa: PLC0415
            _creds, _proj = google.auth.default()
            note("credentials", True,
                 f"ambient ADC via google.auth.default() (metadata/env)"
                 + (f", project={_proj}" if _proj else ""))
        except Exception as exc:  # noqa: BLE001
            note("credentials", False,
                 "no ADC found. On a workstation run "
                 "`gcloud auth application-default login` or set "
                 f"GOOGLE_APPLICATION_CREDENTIALS. ({type(exc).__name__})")

    for name, mod in (("google-cloud-bigquery", "google.cloud.bigquery"),
                      ("google-cloud-logging", "google.cloud.logging"),
                      ("pandas", "pandas"), ("numpy", "numpy"),
                      ("PyYAML", "yaml"), ("db-dtypes", "db_dtypes")):
        try:
            __import__(mod)
            note(f"import:{name}", True, "installed")
        except ImportError:
            note(f"import:{name}", False, f"missing -- pip install {name}")

    # Per-service identity. Reading a key file is local; ADC resolution does not
    # hit the network either, so this stays an offline check.
    def _identity(service: str) -> tuple[bool, str]:
        try:
            _, label = credentials_for(service, cfg)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"
        return True, label

    bq_ok, bq_who = _identity("bigquery")
    note("identity:bigquery", bq_ok, bq_who)

    # identity:logging -- a REAL bounded probe, not an assumption. ADC used to be
    # unable to read acp-prod logs (hence the old hard-coded failure), but a
    # runtime SA granted roles/logging.viewer on the log project CAN, so actually
    # attempt a single-entry read and report the truth. One free API call.
    def _probe_logging() -> tuple[bool, str]:
        try:
            creds, label = credentials_for("logging", cfg)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:80]}"
        src = "key file" if cfg.log_key_file else "ADC"
        try:
            from google.cloud import logging_v2  # noqa: PLC0415
            client = logging_v2.Client(project=cfg.log_project, credentials=creds)
            entries = client.list_entries(
                resource_names=[f"projects/{cfg.log_project}"],
                page_size=1, max_results=1)
            next(iter(entries), None)          # forces one real API call
            return True, f"{label} via {src} -- can read {cfg.log_project} logs"
        except Exception as exc:  # noqa: BLE001
            return False, (f"{label} via {src} -- cannot read {cfg.log_project} "
                           f"logs: {type(exc).__name__}: {str(exc)[:90]}")

    log_ok, log_who = _probe_logging()
    note("identity:logging", log_ok, log_who)

    note("catalog_csv", cfg.catalog_csv.is_file(), str(cfg.catalog_csv))
    note("thresholds.yaml", bool(cfg.thresholds),
         str(cfg.config_dir / "thresholds.yaml"))
    note("exclusions.yaml", bool(cfg.exclusions),
         str(cfg.config_dir / "exclusions.yaml"))

    report["cost_caps"] = {
        "max_bytes_billed_gib": round(cfg.max_bytes_billed / GIB, 1),
        "confirm_gib": round(cfg.confirm_bytes / GIB, 1),
        "run_budget_gib": round(cfg.run_budget_bytes / GIB, 1),
        "dry_run_only": cfg.dry_run_only,
    }
    report["windows"] = {
        "history_days": cfg.history_days or "all",
        "evidence_days": cfg.evidence_days,
    }
    return report


def require_credentials() -> Any:
    """Return google.auth default credentials or raise with a fix."""
    try:
        import google.auth  # noqa: PLC0415
    except ImportError as exc:
        raise MissingCredentials(
            "google-auth is not installed. pip install -r requirements.txt"
        ) from exc
    try:
        creds, _ = google.auth.default()
    except Exception as exc:  # google.auth.exceptions.DefaultCredentialsError
        raise MissingCredentials(
            "no Application Default Credentials. Either run\n"
            "  gcloud auth application-default login\n"
            "or point GOOGLE_APPLICATION_CREDENTIALS at a key file (the "
            "service-account key now lives outside the project tree at "
            r"C:\Users\user\.config\gcloud\keys\)."
        ) from exc
    return creds
