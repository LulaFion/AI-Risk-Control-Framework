-- requires_param: as_of_time
-- description: per (parent x game x playway) profile for a family of operators
--              (@parent_like, e.g. 'v3cnys_%'). Purpose: test the coordinated-
--              cohort hypotheses game by game -- coverage uniformity (does
--              every account play every game?), pooled RTP vs certified per
--              game, bounded activity windows. Reads only through the leakage
--              gate; one row per parent-game-playway downloads.

SELECT
  parent,
  game_id,
  play_type,
  currency,
  COUNT(*)                                        AS rounds,
  COUNT(DISTINCT uid)                             AS players,
  COUNT(DISTINCT DATE(game_time))                 AS active_days,
  SUM(valid_bet)                                  AS turnover,
  SUM(win)                                        AS total_win,
  SAFE_DIVIDE(SUM(win), NULLIF(SUM(valid_bet),0)) AS rtp,
  SUM(valid_bet * valid_bet)                      AS sumsq_valid_bet,
  COUNT(DISTINCT sm_tag)                          AS builds,
  MIN(game_time)                                  AS first_round_utc,
  MAX(game_time)                                  AS last_round_utc
FROM {{rounds_asof}}(@as_of_time)
WHERE parent LIKE @parent_like
GROUP BY parent, game_id, play_type, currency
