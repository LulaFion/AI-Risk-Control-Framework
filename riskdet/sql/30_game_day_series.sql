-- requires_param: as_of_time
-- description: per (peer-cell x UTC event date) daily series for Layer 2 --
--              game-level drift detection ("did the GAME start paying wrong"),
--              the complement to Layer 1's "is this PLAYER extracting value".
--              Reads only through the leakage gate; one row per cell-day is
--              what downloads. Grouping is by DATE(game_time) (UTC event date)
--              so the series aligns with the replay boundary; report_date
--              (UTC+8 business date) rides along for human reporting only.

SELECT
  game_id,
  play_type,
  currency,
  sm_tag,
  DATE(game_time)                                 AS event_date,
  MAX(report_date)                                AS report_date_utc8,
  COUNT(*)                                        AS rounds,
  COUNT(DISTINCT CONCAT(parent, ':', uid))        AS n_players,
  SUM(valid_bet)                                  AS turnover,
  SUM(win)                                        AS total_win,
  SUM(valid_bet * valid_bet)                      AS sumsq_valid_bet,
  COUNTIF(win > 0)                                AS n_win,
  COUNTIF(triggered)                              AS n_trigger,
  COUNTIF(sm_v = 'feature_beta')                  AS beta_rounds,
  -- day-level concentration ingredients: the single biggest winner's net take,
  -- to separate "the math broke" from "one account drained it"
  MAX(win)                                        AS max_round_win
FROM {{rounds_asof}}(@as_of_time)
GROUP BY game_id, play_type, currency, sm_tag, event_date
