-- requires_param: as_of_time
-- description: per (player, peer-cell) sufficient statistics. Reads ONLY through
--              the rounds_asof leakage gate. Raw rounds never leave BigQuery --
--              this aggregate (one row per player-cell) is what gets downloaded.
--
-- The peer cell is (game_id, play_type, currency, sm_tag). Because play_type is
-- IN the key, "trigger rate within base spins" and "bet stats within one
-- playway" fall out of the grain naturally instead of needing filtered rules.
--
-- PRESCIENT_BET needs no raw rounds despite being a permutation test: under
-- exchangeability the permutation null of mean(bet | triggered) is a simple
-- random sample of size k from the player's bet vector, so its exact mean and
-- variance follow from (n, k, sum_bet, sumsq_bet, sum_bet_trigger) -- all here.

-- Two stages: BigQuery rejects an aggregate inside UNNEST, so the ARRAY_AGG
-- lands in a column first and is formatted in the outer SELECT.
WITH agg AS (
SELECT
  parent,
  uid,
  game_id,
  play_type,
  currency,
  sm_tag,
  COUNT(*)                                        AS rounds,
  SUM(valid_bet)                                  AS turnover,
  SUM(win)                                        AS total_win,
  COUNTIF(win > 0)                                AS n_win,
  COUNTIF(triggered)                              AS n_trigger,
  -- L1-DUP_ROUND: same game_seq_id settled more than once. >0 here is either a
  -- double-settle or an ETL duplicate -- only the spin-server response count in
  -- Cloud Logging can tell which, so this is always Layer-4-adjudicated.
  COUNT(*) - COUNT(DISTINCT game_seq_id)          AS dup_seq_rounds,
  COUNT(DISTINCT bet)                             AS bet_levels,
  SUM(bet)                                        AS sum_bet,
  SUM(bet * bet)                                  AS sumsq_bet,
  SUM(IF(triggered, bet, 0))                      AS sum_bet_trigger,
  -- Var(total_win - r*turnover) = SUM(valid_bet_i^2) * sigma_spin^2 when bets
  -- are UNEQUAL. Using mean_bet * sqrt(n) instead understates the SE whenever
  -- stakes vary (they do: feature buys are 32.5-300x base), which manufactures
  -- significance. sumsq_valid_bet is the correct variance weight.
  SUM(valid_bet * valid_bet)                      AS sumsq_valid_bet,
  -- zero-inflation handled by the hurdle split: log-multiple over wins only
  SUM(IF(win > 0, LN(SAFE_DIVIDE(win, valid_bet)), 0))       AS sum_log_mult,
  SUM(IF(win > 0, POW(LN(SAFE_DIVIDE(win, valid_bet)), 2), 0)) AS sumsq_log_mult,
  MAX(SAFE_DIVIDE(win, valid_bet))                AS max_multiple,
  -- L1-BALANCE_IDENTITY: same-row identity, immune to deposits/session gaps.
  -- Relative tolerance because MMK balances are ~1e5 and USD ~1e0.
  COUNTIF(ABS(after_balance - (before_balance - bet + win))
          > GREATEST(0.01, ABS(before_balance) * 1e-6))      AS balance_violations,
  MIN(game_time)                                  AS first_round_utc,
  MAX(game_time)                                  AS last_round_utc,
  COUNT(DISTINCT DATE(game_time))                 AS active_days,
  -- evidence rounds, formatted in the outer stage
  ARRAY_AGG(STRUCT(game_seq_id, game_time, win)
            ORDER BY win DESC LIMIT 5)            AS top_rounds
FROM {{rounds_asof}}(@as_of_time)
GROUP BY parent, uid, game_id, play_type, currency, sm_tag
HAVING rounds >= @min_rounds
)
SELECT
  agg.* EXCEPT (top_rounds),
  -- evidence keys: gsid@UTC so the log puller opens +/-1 min per round
  ARRAY_TO_STRING(ARRAY(
    SELECT FORMAT('%s@%s', game_seq_id,
                  FORMAT_DATETIME('%Y-%m-%dT%H:%M:%SZ', game_time))
    FROM UNNEST(top_rounds)), '|')                AS top_win_seq_ids
FROM agg
