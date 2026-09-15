-- requires_param: as_of_time
-- description: per-SECOND aggregates for ONE case -- the cadence evidence view
--              for burst/automation cases. One row per active second (counts
--              and sums only; raw rounds stay in BigQuery). game_time is
--              1-second UTC, so this is the finest grain BigQuery supports;
--              anything sub-second would need Cloud Logging.

SELECT
  game_time                                        AS sec_utc,
  COUNT(*)                                         AS rounds,
  SUM(valid_bet)                                   AS turnover,
  SUM(win)                                         AS total_win,
  COUNTIF(triggered)                               AS n_trigger,
  MAX(SAFE_DIVIDE(win, valid_bet))                 AS max_multiple
FROM {{rounds_asof}}(@as_of_time)
WHERE parent = @parent AND uid = @uid
GROUP BY sec_utc
ORDER BY sec_utc
