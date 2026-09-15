-- description: FAST integrity-only scan of the NEWEST source rounds, for a
--              high-cadence (per-minute) loop. ABSOLUTE integrity rules only --
--              MAX_X_BREACH, BALANCE_IDENTITY, DUP_ROUND -- because those are
--              deterministic, single-round-true, and need no calibration,
--              history, peer baseline, or the min_rounds floor. STATISTICAL
--              rules are deliberately excluded here (a one-minute sample cannot
--              support them; they belong in the daily `run`).
--
-- Cost model: reads {{source_table}} filtered by `serial > @serial_watermark`.
-- serial LEADS the source clustering, so this predicate is what prunes source
-- blocks. As with the increment, the DRY-RUN ESTIMATE is an UPPER BOUND that
-- ignores cluster pruning (~full-column bytes) -- judge real cost by the
-- LEDGER's billed bytes, not the estimate. Run with allow_large=True.
--
-- Returns: exactly one GUARANTEED summary row (scan_max_serial, scan_rows) so
-- the watermark can advance even on a clean scan, LEFT JOINed to zero-or-more
-- violation cells (case columns are NULL on the summary-only row).
--
-- Time semantics: game_time = UTC event time; ReportDate = UTC+8 business date,
-- never used here. Demo traffic excluded exactly as the ingest does.

WITH new_rows AS (
  SELECT
    parent, uid, game_id, play_type, currency, sm_tag,
    serial, game_seq_id, game_time,
    CAST(bet            AS FLOAT64) AS bet,
    CAST(validBet       AS FLOAT64) AS valid_bet,
    CAST(win            AS FLOAT64) AS win,
    CAST(before_balance AS FLOAT64) AS before_balance,
    CAST(after_balance  AS FLOAT64) AS after_balance
  FROM {{source_table}}
  WHERE test_demo_play = 'False'
    AND parent <> 'acdemo'
    AND serial > @serial_watermark
),

agg AS (
  SELECT
    parent, uid, game_id, play_type, currency, sm_tag,
    COUNT(*)                                        AS rounds,
    -- L1-BALANCE_IDENTITY: same-row identity, relative tolerance (MMK ~1e5).
    COUNTIF(ABS(after_balance - (before_balance - bet + win))
            > GREATEST(0.01, ABS(before_balance) * 1e-6))   AS balance_violations,
    -- L1-DUP_ROUND: same game_seq_id settled more than once IN THIS SLICE.
    COUNT(*) - COUNT(DISTINCT game_seq_id)          AS dup_seq_rounds,
    -- L1-MAX_X_BREACH pre-filter metric; exact per-(game,play_type) cert cap is
    -- applied in Python from the certified sheet (not available in BigQuery).
    MAX(SAFE_DIVIDE(win, valid_bet))                AS max_multiple,
    MAX(serial)                                     AS grp_max_serial,
    ARRAY_AGG(STRUCT(game_seq_id, game_time, win)
              ORDER BY win DESC LIMIT 5)            AS top_rounds
  FROM new_rows
  GROUP BY parent, uid, game_id, play_type, currency, sm_tag
),

summary AS (
  -- always exactly one row, so the orchestrator can advance the watermark even
  -- when there are zero violations (or zero new rows).
  SELECT
    COALESCE(MAX(grp_max_serial), @serial_watermark) AS scan_max_serial,
    COALESCE(SUM(rounds), 0)                         AS scan_rows
  FROM agg
),

viol AS (
  SELECT
    * EXCEPT (top_rounds),
    ARRAY_TO_STRING(ARRAY(
      SELECT FORMAT('%s@%s', game_seq_id,
                    FORMAT_DATETIME('%Y-%m-%dT%H:%M:%SZ', game_time))
      FROM UNNEST(top_rounds)), '|')                AS top_win_seq_ids
  FROM agg
  WHERE balance_violations > 0
     OR dup_seq_rounds > 0
     OR max_multiple >= @min_cert_max
)

SELECT s.scan_max_serial, s.scan_rows, v.*
FROM summary s
LEFT JOIN viol v ON TRUE
