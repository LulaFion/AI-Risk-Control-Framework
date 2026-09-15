-- requires_param: as_of_time
-- description: per-day aggregates for ONE case (parent, uid, game). Investigator
--              drill-down: daily volume, money, trigger rate, and the stake
--              split between triggering and non-triggering rounds. Aggregates
--              only -- raw rounds stay in BigQuery. Cheap: the archive is
--              clustered on (parent, uid).

SELECT
  DATE(game_time)                                  AS event_date,
  COUNT(*)                                         AS rounds,
  SUM(valid_bet)                                   AS turnover,
  SUM(win)                                         AS total_win,
  COUNTIF(triggered)                               AS n_trigger,
  SAFE_DIVIDE(SUM(IF(triggered, bet, 0)),
              NULLIF(COUNTIF(triggered), 0))       AS avg_bet_trigger,
  SAFE_DIVIDE(SUM(IF(NOT triggered, bet, 0)),
              NULLIF(COUNTIF(NOT triggered), 0))   AS avg_bet_no_trigger,
  MAX(SAFE_DIVIDE(win, valid_bet))                 AS max_multiple
FROM {{rounds_asof}}(@as_of_time)
WHERE parent = @parent AND uid = @uid AND game_id = @game_id
  AND play_type = 0
GROUP BY event_date
ORDER BY event_date
