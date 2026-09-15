-- requires_param: as_of_time
-- description: stake-level x outcome histogram for ONE case -- the direct
--              visual evidence for PRESCIENT_BET: under honest math the stake
--              chosen for a round is independent of whether it triggers, so
--              the two stake distributions must match. Aggregates only.

SELECT
  bet,
  triggered,
  COUNT(*)      AS rounds,
  SUM(win)      AS total_win,
  SUM(valid_bet) AS turnover
FROM {{rounds_asof}}(@as_of_time)
WHERE parent = @parent AND uid = @uid AND game_id = @game_id
  AND play_type = 0
GROUP BY bet, triggered
ORDER BY bet, triggered
