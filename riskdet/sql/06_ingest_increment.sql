-- description: IDEMPOTENT incremental ingestion. Appends new rounds and updates
--              late-arriving corrections into the archive without deleting or
--              rewriting older history -- September and beyond land here; the
--              bootstrap (05) is never re-run.
--
-- MERGE key: serial (unique per settled round; the source's primary key is
-- (serial, game_time)). Idempotency: re-running with the same watermark
-- re-matches the same rows -- WHEN MATCHED only updates when the source row is
-- genuinely NEWER (last_modify_time), so a replayed increment is a no-op, and
-- WHEN NOT MATCHED inserts exactly once. Historical partitions are preserved:
-- MERGE touches only matched/new rows, never truncates.
--
-- Watermarks (both mandatory, supplied by the orchestrator from the archive):
--   @serial_watermark   MAX(serial) already ingested, minus a safety margin.
--                       serial leads the source clustering, so this predicate
--                       is the one that CAN prune source blocks. The dry-run
--                       estimate will still show ~full-column bytes (estimates
--                       ignore cluster pruning: "upper bound"); judge by the
--                       LEDGER's billed bytes on the first real run. If billed
--                       ~= full scan, clustering is not helping and the fall-
--                       back is a monthly full re-merge at ~$0.70 -- decided
--                       from data, recorded in the ledger, never guessed.
--   @modified_since     lower bound on last_modify_time, catching corrections
--                       to rows whose serial is BELOW the watermark.
--
-- Time semantics identical to the bootstrap: game_time = UTC event time;
-- ReportDate = UTC+8 business date, never a boundary.

MERGE {{rounds_table}} AS t
USING (
  SELECT
    game_time,
    ReportDate       AS report_date,
    serial,
    game_seq_id,
    parent,
    uid,
    game_id,
    play_type,
    currency,
    sm_v,
    sm_tag,
    CAST(bet            AS FLOAT64) AS bet,
    CAST(validBet       AS FLOAT64) AS valid_bet,
    CAST(win            AS FLOAT64) AS win,
    CAST(before_balance AS FLOAT64) AS before_balance,
    CAST(after_balance  AS FLOAT64) AS after_balance,
    (hasFreegame = 'True')          AS triggered,
    last_modify_time
  FROM {{source_table}}
  WHERE test_demo_play = 'False'
    AND parent <> 'acdemo'
    AND (serial > @serial_watermark
         OR last_modify_time >= @modified_since)
) AS s
ON t.serial = s.serial
WHEN MATCHED AND s.last_modify_time > t.last_modify_time THEN UPDATE SET
  game_time        = s.game_time,
  report_date      = s.report_date,
  game_seq_id      = s.game_seq_id,
  parent           = s.parent,
  uid              = s.uid,
  game_id          = s.game_id,
  play_type        = s.play_type,
  currency         = s.currency,
  sm_v             = s.sm_v,
  sm_tag           = s.sm_tag,
  bet              = s.bet,
  valid_bet        = s.valid_bet,
  win              = s.win,
  before_balance   = s.before_balance,
  after_balance    = s.after_balance,
  triggered        = s.triggered,
  last_modify_time = s.last_modify_time
WHEN NOT MATCHED THEN INSERT (
  game_time, report_date, serial, game_seq_id, parent, uid, game_id,
  play_type, currency, sm_v, sm_tag, bet, valid_bet, win,
  before_balance, after_balance, triggered, last_modify_time
) VALUES (
  s.game_time, s.report_date, s.serial, s.game_seq_id, s.parent, s.uid,
  s.game_id, s.play_type, s.currency, s.sm_v, s.sm_tag, s.bet, s.valid_bet,
  s.win, s.before_balance, s.after_balance, s.triggered, s.last_modify_time
)
