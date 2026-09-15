-- requires_param: as_of_time
-- description: the specific rounds where the same-row balance identity fails
--              (after != before - bet + win) for ONE case. Returns ONLY the
--              violating rounds -- targeted evidence rows with the gsid@time
--              key for a Cloud Logging pull, not a bulk download.

SELECT
  game_seq_id,
  game_time,
  game_id,
  play_type,
  bet,
  valid_bet,
  win,
  before_balance,
  after_balance,
  after_balance - (before_balance - bet + win) AS identity_gap
FROM {{rounds_asof}}(@as_of_time)
WHERE parent = @parent AND uid = @uid
  AND ABS(after_balance - (before_balance - bet + win))
      > GREATEST(0.01, ABS(before_balance) * 1e-6)
ORDER BY game_time
