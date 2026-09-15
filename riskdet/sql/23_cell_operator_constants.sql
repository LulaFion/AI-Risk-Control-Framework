-- requires_param: as_of_time
-- description: per (peer-cell x operator) sufficient statistics, enabling EXACT
--              leave-operator-out and leave-cohort-out baselines:
--                  p0_excl_op = (cell_total - operator_total)
--                             / (cell_rounds - operator_rounds)
--              Without this, an operator (or a bot cohort inside one) sets its
--              own baseline and only its own tail pops -- the selection-bias
--              failure observed in the prior iteration. One row per
--              cell x parent; tiny; only aggregates leave BigQuery.

SELECT
  game_id,
  play_type,
  currency,
  sm_tag,
  parent,
  COUNT(*)                                        AS rounds,
  COUNT(DISTINCT uid)                             AS n_players,
  SUM(valid_bet)                                  AS turnover,
  SUM(win)                                        AS total_win,
  COUNTIF(win > 0)                                AS n_win,
  COUNTIF(triggered)                              AS n_trigger,
  SUM(valid_bet * valid_bet)                      AS sumsq_valid_bet
FROM {{rounds_asof}}(@as_of_time)
GROUP BY game_id, play_type, currency, sm_tag, parent
