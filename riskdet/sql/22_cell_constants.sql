-- requires_param: as_of_time
-- description: per peer-cell POPULATION constants -- the denominators of every
--              rate test and the round-grain dispersion that turnover-weights
--              the money test. Computed at round grain in BigQuery because raw
--              rounds never come down; one row per cell is what downloads.
--
--              sigma_spin = per-round SD of win/valid_bet. It is what makes a
--              500-round day and a 500k-round day incomparable on the same
--              scale -- the z for excess money uses sigma_spin / sqrt(n).

SELECT
  game_id,
  play_type,
  currency,
  sm_tag,
  COUNT(*)                                        AS rounds,
  COUNT(DISTINCT CONCAT(parent, ':', uid))        AS n_players,
  SUM(valid_bet)                                  AS turnover,
  SAFE_DIVIDE(SUM(win), NULLIF(SUM(valid_bet),0)) AS rtp_emp,
  STDDEV_SAMP(SAFE_DIVIDE(win, valid_bet))        AS sigma_spin,
  SAFE_DIVIDE(COUNTIF(win > 0), COUNT(*))         AS p_hit,
  SAFE_DIVIDE(COUNTIF(triggered), COUNT(*))       AS p_trigger,
  -- L1-BETA_IN_PROD: beta math serving real money. An attribute of the BUILD
  -- (game-level finding), never player evidence -- sm_v varies within sm_tag.
  COUNTIF(sm_v = 'feature_beta')                  AS beta_rounds,
  AVG(IF(win > 0, LN(SAFE_DIVIDE(win, valid_bet)), NULL))         AS mean_log_mult,
  STDDEV_SAMP(IF(win > 0, LN(SAFE_DIVIDE(win, valid_bet)), NULL)) AS sd_log_mult,
  MAX(SAFE_DIVIDE(win, valid_bet))                AS max_multiple_observed,
  MIN(game_time)                                  AS first_round_utc,
  MAX(game_time)                                  AS last_round_utc
FROM {{rounds_asof}}(@as_of_time)
GROUP BY game_id, play_type, currency, sm_tag
