-- measured_bytes: 117094367761
-- description: ONE-TIME BOOTSTRAP of the rounds archive. Scans the unpartitioned
--              source exactly once and preserves the full May-August history in
--              a partitioned BigQuery table. Never run twice: CREATE TABLE IF
--              NOT EXISTS refuses to clobber, and the orchestrator additionally
--              checks existence before submitting. All later data arrives via
--              06_ingest_increment.sql (MERGE), which appends/updates without
--              deleting history.
--
-- WHY THE FULL HISTORY IN ONE SCAN: the source is clustered on
-- (serial, game_time) and NOT date-partitioned. Measured 2026-08-18: a 30-day
-- ReportDate filter and no filter both estimate ~113 GB for this column set --
-- a date predicate prunes NOTHING, so August is free to include and expensive
-- to go back for. It is included physically and hidden logically:
-- every replay/downstream query is bounded by a mandatory @as_of_time on
-- event time (see 07_replay_tvf.sql), which simulates hourly/daily arrival
-- without ever rescanning the source.
--
-- TIME SEMANTICS -- two clocks, kept separate on purpose:
--   game_time   DATETIME, UTC, 1-second resolution. THE event time. The replay
--               boundary, partitioning, and all leakage control use this.
--   ReportDate  DATE, UTC+8 BUSINESS date (rolls at 16:00 UTC). Grouping /
--               reporting only. NEVER a replay boundary: one ReportDate spans
--               two UTC calendar days, so filtering on it leaks future events
--               across the boundary.
--
-- PARTITIONING: by DATE(game_time) (event date, UTC), so `game_time <=
-- @as_of_time` prunes partitions. Clustered by (parent, uid) so per-player
-- drill-downs read only their blocks.
--
-- WINDOW ROLES (config/thresholds.yaml `windows:`):
--   May 2026            calibration / training
--   June-July 2026      chronological validation (walk-forward, event time)
--   August 2026         hidden holdout, replayed incrementally via as_of_time
--
-- Demo traffic is excluded at the source: not real money, nothing downstream
-- wants it. QA fleets are NOT excluded here -- exclusion requires the approved
-- list (config/exclusions.yaml); structural fleet candidates are quarantined
-- downstream instead.

CREATE TABLE IF NOT EXISTS {{rounds_table}}
PARTITION BY DATE(game_time)
CLUSTER BY parent, uid
OPTIONS (
  description = 'riskdet rounds archive. Bootstrapped once from OMG.RecordSlot;'
                ' maintained by MERGE increments only. Partition = UTC event'
                ' date of game_time. ReportDate is the UTC+8 business date and'
                ' must never be used as a replay boundary.'
)
AS
SELECT
  game_time,                                    -- UTC event time (DATETIME)
  ReportDate       AS report_date,              -- UTC+8 business date
  serial,                                       -- unique row key (MERGE key)
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
  (hasFreegame = 'True')          AS triggered, -- STRING in source, BOOL here
  last_modify_time                              -- late-arrival ordering for MERGE
FROM {{source_table}}
WHERE test_demo_play = 'False'
  AND parent <> 'acdemo'
