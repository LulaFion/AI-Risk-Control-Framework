"""CostGuardedBQ -- the only way this package talks to BigQuery.

Every query, without exception, goes through query_df()/query_job(), which run
the mandatory sequence:

    1. dry run (free)                        -> CostEstimate
    2. ledger the estimate
    3. regression brake: estimate vs the SQL file's measured_bytes header
    4. per-query cap  (cfg.max_bytes_billed) -> CostBudgetExceeded
    5. confirmation gate (cfg.confirm_bytes) -> ConfirmationRequired
    6. dry_run_only mode                     -> stop here, spend nothing
    7. RunBudget.reserve(estimate)           -> cumulative cap
    8. real job with maximum_bytes_billed    -> BigQuery hard-fails, not bills
    9. ledger the ACTUAL billed bytes + job id

There is intentionally no re-export of the raw google client. A contributor who
wants an unguarded query has to import google.cloud.bigquery directly, which is
greppable and test-enforced (tests/test_cost_guard.py).

The free paths -- tabledata.list and table metadata -- are exposed separately
because they create no job and bill nothing; they are how fixtures get built.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..config import Settings, credentials_for, settings
from ..errors import ConfirmationRequired, CostBudgetExceeded, DataQualityError
from . import cost as _cost
from . import sql as _sql

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd

log = logging.getLogger("riskdet.bq")

# An edited query may legitimately grow a little (schema drift, extra column).
# Growing past measured_bytes * this factor is treated as a regression and
# refused until the header is deliberately updated.
_MEASURED_TOLERANCE = 1.25


class CostGuardedBQ:
    def __init__(self, cfg: Settings | None = None) -> None:
        from google.cloud import bigquery  # noqa: PLC0415

        self.cfg = cfg or settings()
        creds, identity = credentials_for("bigquery", self.cfg)
        self.identity = identity
        self._bq = bigquery
        self._client = bigquery.Client(
            credentials=creds, project=self.cfg.bq_project,
            location=self.cfg.bq_location)
        self.budget = _cost.RunBudget(limit_bytes=self.cfg.run_budget_bytes)

    # ------------------------------------------------------------------ #
    # estimation (free)                                                   #
    # ------------------------------------------------------------------ #
    def estimate(self, loaded: _sql.LoadedSQL,
                 params: dict[str, Any] | None = None) -> _cost.CostEstimate:
        """Dry run. Free. use_query_cache=False so the estimate is worst-case."""
        job_config = self._bq.QueryJobConfig(
            dry_run=True, use_query_cache=False,
            query_parameters=_sql.to_query_parameters(params))
        job = self._client.query(loaded.text, job_config=job_config)
        nbytes = int(job.total_bytes_processed or 0)
        referenced = tuple(
            f"{t.project}.{t.dataset_id}.{t.table_id}"
            for t in (job.referenced_tables or []))
        return _cost.CostEstimate(
            sql_name=loaded.name,
            bytes_processed=nbytes,
            usd=self.cfg.usd(nbytes),
            cache_hit=bool(getattr(job, "cache_hit", False)),
            referenced_tables=referenced)

    # ------------------------------------------------------------------ #
    # the guarded query path                                              #
    # ------------------------------------------------------------------ #
    def query_df(self, sql_name: str,
                 params: dict[str, Any] | None = None,
                 *,
                 identifiers: dict[str, str] | None = None,
                 max_bytes: int | None = None,
                 allow_large: bool = False,
                 destination: str | None = None,
                 write_disposition: str = "WRITE_TRUNCATE",
                 stage: str = "adhoc") -> "pd.DataFrame":
        """Run riskdet/sql/<sql_name> through the full guard, return a frame.

        With destination=... the result is materialised to a table instead and
        an empty frame is returned (the data stays in BigQuery; that is the
        point of materialising).
        """
        import pandas as pd  # noqa: PLC0415

        loaded = _sql.load(sql_name, identifiers=self._default_identifiers(identifiers))
        loaded.check_params(params)   # the replay-boundary gate, before anything
        cap = max_bytes if max_bytes is not None else self.cfg.max_bytes_billed
        ledger = self.cfg.paths.cost_ledger

        # 1-2: estimate + ledger
        est = self.estimate(loaded, params)
        log.info("%s [stage=%s]", est, stage)
        _cost.append_ledger(ledger, sql_name=loaded.name, status="estimated",
                            estimate=est, usd_per_tib=self.cfg.usd_per_tib,
                            identity=self.identity, note=stage)

        # 3: regression brake against the file's measured_bytes header
        if (loaded.measured_bytes is not None
                and est.bytes_processed > loaded.measured_bytes * _MEASURED_TOLERANCE):
            _cost.append_ledger(ledger, sql_name=loaded.name, status="refused",
                                estimate=est, usd_per_tib=self.cfg.usd_per_tib,
                                identity=self.identity,
                                note=(f"regression: measured_bytes="
                                      f"{loaded.measured_bytes}"))
            raise CostBudgetExceeded(
                loaded.name, est.bytes_processed,
                int(loaded.measured_bytes * _MEASURED_TOLERANCE),
                scope="measured-regression")

        # 4: per-query hard cap
        if est.bytes_processed > cap:
            _cost.append_ledger(ledger, sql_name=loaded.name, status="refused",
                                estimate=est, usd_per_tib=self.cfg.usd_per_tib,
                                identity=self.identity, note="per-query cap")
            raise CostBudgetExceeded(loaded.name, est.bytes_processed, cap)

        # 5: confirmation gate
        if est.bytes_processed > self.cfg.confirm_bytes and not allow_large:
            _cost.append_ledger(ledger, sql_name=loaded.name, status="refused",
                                estimate=est, usd_per_tib=self.cfg.usd_per_tib,
                                identity=self.identity, note="needs allow_large")
            raise ConfirmationRequired(
                loaded.name, est.bytes_processed, self.cfg.confirm_bytes, est.usd)

        # 6: dry-run-only mode -- validate everything, execute nothing
        if self.cfg.dry_run_only:
            log.info("RISKDET_DRY_RUN_ONLY=1 -- not executing %s", loaded.name)
            _cost.append_ledger(ledger, sql_name=loaded.name, status="dry_run_only",
                                estimate=est, usd_per_tib=self.cfg.usd_per_tib,
                                identity=self.identity, note=stage)
            return pd.DataFrame()

        # 7: cumulative run budget
        self.budget.reserve(est.bytes_processed, loaded.name)

        # 8: the real job, with the server-side kill switch
        job_config = self._bq.QueryJobConfig(
            maximum_bytes_billed=cap,
            query_parameters=_sql.to_query_parameters(params),
            labels={"app": "riskdet", "stage": stage[:60].lower()})
        if destination:
            job_config.destination = destination
            job_config.write_disposition = write_disposition
        try:
            job = self._client.query(loaded.text, job_config=job_config)
            result = job.result()
        except Exception:
            self.budget.refund(est.bytes_processed)
            _cost.append_ledger(ledger, sql_name=loaded.name, status="failed",
                                estimate=est, usd_per_tib=self.cfg.usd_per_tib,
                                identity=self.identity, note=stage)
            raise

        # 9: reconcile the actual
        billed = int(job.total_bytes_billed or 0)
        if billed < est.bytes_processed:
            self.budget.refund(est.bytes_processed - billed)
        _cost.append_ledger(ledger, sql_name=loaded.name, status="done",
                            estimate=est, billed_bytes=billed,
                            usd_per_tib=self.cfg.usd_per_tib,
                            job_id=job.job_id, identity=self.identity,
                            note=stage)
        log.info("%s billed %s (job %s)", loaded.name,
                 _cost.human(billed), job.job_id)

        if destination:
            return pd.DataFrame()
        return result.to_dataframe()

    # ------------------------------------------------------------------ #
    # free paths -- no job, no billing                                    #
    # ------------------------------------------------------------------ #
    def list_rows_df(self, table: str, columns: list[str],
                     max_results: int, start_index: int | None = None
                     ) -> "pd.DataFrame":
        """tabledata.list -- reads stored rows without creating a query job.

        This is the fixture-building path: free at any volume, but it reads
        rows in storage order (serial-clustered here), not by predicate.
        """
        t = self._client.get_table(table)
        field_map = {f.name: f for f in t.schema}
        missing = [c for c in columns if c not in field_map]
        if missing:
            raise DataQualityError(f"{table} has no column(s) {missing}")
        rows = self._client.list_rows(
            t, selected_fields=[field_map[c] for c in columns],
            max_results=max_results, start_index=start_index)
        return rows.to_dataframe()

    def table_bytes(self, table: str) -> int:
        """Metadata only -- free."""
        return int(self._client.get_table(table).num_bytes or 0)

    def table_num_rows(self, table: str) -> int:
        return int(self._client.get_table(table).num_rows or 0)

    def ensure_dataset(self, dataset: str | None = None) -> str:
        """Create the working dataset if absent. Location must match the
        source table (asia-southeast1) or every query cross-region-fails."""
        ds_id = f"{self.cfg.bq_project}.{dataset or self.cfg.work_dataset}"
        ds = self._bq.Dataset(ds_id)
        ds.location = self.cfg.bq_location
        self._client.create_dataset(ds, exists_ok=True)
        return ds_id

    # ------------------------------------------------------------------ #
    def _default_identifiers(self, extra: dict[str, str] | None
                             ) -> dict[str, str]:
        base = {
            "source_table": self.cfg.source_table,
            "rounds_table": self.cfg.work_table_rounds,
            "dataset": f"{self.cfg.bq_project}.{self.cfg.work_dataset}",
            # every analytical query reads rounds through the leakage gate:
            "rounds_asof": f"{self.cfg.bq_project}.{self.cfg.work_dataset}.rounds_asof",
        }
        if extra:
            base.update(extra)
        return base
