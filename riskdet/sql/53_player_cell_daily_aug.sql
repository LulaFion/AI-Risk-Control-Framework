-- requires_param: as_of_time
-- description: per (player, cell, DAY) additive sufficient statistics for the
--              August-1..7 week only (reads just those partitions of w1 -- cheap).
--              Same additive fields as 20_player_cell_features.sql but grouped by
--              event day, so offline roll-up gives EXACT cumulative features at
--              any as_of: baseline(07-31) + sum(daily rows <= D). Injected rows
--              are in w1, so they appear here automatically on their real days.
--              No min_rounds HAVING -- daily cells are small by design.
WITH daily AS (
  SELECT
    parent, uid, game_id, play_type, currency, sm_tag,
    DATE(game_time)                               AS event_date,
    COUNT(*)                                      AS rounds,
    SUM(valid_bet)                                AS turnover,
    SUM(win)                                      AS total_win,
    COUNTIF(win > 0)                              AS n_win,
    COUNTIF(triggered)                            AS n_trigger,
    COUNT(*) - COUNT(DISTINCT game_seq_id)        AS dup_seq_rounds,
    COUNT(DISTINCT bet)                           AS bet_levels_day,
    SUM(bet)                                      AS sum_bet,
    SUM(bet * bet)                                AS sumsq_bet,
    SUM(IF(triggered, bet, 0))                    AS sum_bet_trigger,
    SUM(valid_bet * valid_bet)                    AS sumsq_valid_bet,
    SUM(IF(win > 0, LN(SAFE_DIVIDE(win, valid_bet)), 0))       AS sum_log_mult,
    SUM(IF(win > 0, POW(LN(SAFE_DIVIDE(win, valid_bet)), 2), 0)) AS sumsq_log_mult,
    MAX(SAFE_DIVIDE(win, valid_bet))              AS max_multiple,
    COUNTIF(ABS(after_balance - (before_balance - bet + win))
            > GREATEST(0.01, ABS(before_balance) * 1e-6))    AS balance_violations,
    MIN(game_time)                                AS first_round_utc,
    MAX(game_time)                                AS last_round_utc,
    ARRAY_AGG(STRUCT(game_seq_id, game_time, win) ORDER BY win DESC LIMIT 5) AS top_rounds
  FROM {{rounds_asof}}(@as_of_time)
  WHERE game_time >= DATETIME '2026-08-01T00:00:00'
  GROUP BY parent, uid, game_id, play_type, currency, sm_tag, event_date
)
SELECT
  * EXCEPT (top_rounds),
  ARRAY_TO_STRING(ARRAY(
    SELECT FORMAT('%s@%s', game_seq_id,
                  FORMAT_DATETIME('%Y-%m-%dT%H:%M:%SZ', game_time))
    FROM UNNEST(top_rounds)), '|')                AS top_win_seq_ids
FROM daily
