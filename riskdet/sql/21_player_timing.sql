-- requires_param: as_of_time
-- description: per-PLAYER timing statistics (cadence is a property of the
--              player's hands, not of one game, so the grain here is
--              (parent, uid) across all cells). Reads only through the
--              rounds_asof leakage gate; only this aggregate is downloaded.
--
-- game_time is 1-second UTC. Sub-second facts do not exist here (they live in
-- Cloud Logging `duration`), so these statistics are honest at 1s grain:
--   same-second share   the only sub-second fact the grid contains
--   modal gap share     shape of the integer gap distribution in [1,30]s --
--                       a scripted client is near-degenerate, autoplay merely
--                       concentrated; the threshold comes from calibration
--   duty cycle          hours-of-day covered / max idle / active days
--   rate ceiling        max rounds per minute + minutes at superhuman rate
--   concurrency         gap<=0 across DIFFERENT games = >1 client on the uid

WITH seq AS (
  SELECT
    parent, uid, game_id, game_time,
    valid_bet, win,
    DATETIME_DIFF(game_time,
      LAG(game_time) OVER w, SECOND)              AS gap_s,
    (game_id != LAG(game_id) OVER w)              AS game_changed
  FROM {{rounds_asof}}(@as_of_time)
  WINDOW w AS (PARTITION BY parent, uid ORDER BY game_time, serial)
),

sess AS (  -- synthetic sessions: no session_id exists in the source
  SELECT *,
    SUM(IF(gap_s IS NULL OR gap_s > @session_idle_s, 1, 0))
      OVER (PARTITION BY parent, uid ORDER BY game_time) AS session_no
  FROM seq
),

-- BEH_5 ancestry: tiny sessions that run hot (ratio -> currency-neutral)
hot AS (
  SELECT parent, uid,
         COUNTIF(n < 30 AND stake > 0 AND won > 2.0 * stake) AS hot_small_sessions,
         COUNT(*) AS sessions
  FROM (SELECT parent, uid, session_no, COUNT(*) n,
               SUM(valid_bet) stake, SUM(win) won
        FROM sess GROUP BY parent, uid, session_no)
  GROUP BY parent, uid
),

-- modal integer gap in [1, 30] s
modal AS (
  SELECT parent, uid, gap_s AS modal_gap_s, cnt AS modal_count
  FROM (
    SELECT parent, uid, gap_s, COUNT(*) cnt,
           ROW_NUMBER() OVER (PARTITION BY parent, uid
                              ORDER BY COUNT(*) DESC) rn
    FROM seq WHERE gap_s BETWEEN 1 AND 30
    GROUP BY parent, uid, gap_s)
  WHERE rn = 1
),

per_minute AS (
  SELECT parent, uid,
         MAX(n_min)                    AS max_rounds_per_minute,
         COUNTIF(n_min >= @physical_rpm) AS superhuman_minutes
  FROM (SELECT parent, uid, DATETIME_TRUNC(game_time, MINUTE) m, COUNT(*) n_min
        FROM seq GROUP BY parent, uid, m)
  GROUP BY parent, uid
)

SELECT
  s.parent, s.uid,
  COUNT(*)                                          AS rounds,
  COUNTIF(s.gap_s IS NOT NULL)                      AS n_gaps,
  COUNTIF(s.gap_s = 0)                              AS same_second_gaps,
  COUNTIF(s.gap_s BETWEEN 1 AND 30)                 AS in_range_gaps,
  ANY_VALUE(m.modal_gap_s)                          AS modal_gap_s,
  SAFE_DIVIDE(ANY_VALUE(m.modal_count),
              NULLIF(COUNTIF(s.gap_s BETWEEN 1 AND 30), 0)) AS modal_share,
  COUNTIF(s.gap_s <= 0 AND s.game_changed)          AS concurrent_rounds,
  MAX(IF(s.gap_s <= @gap_cap_s, s.gap_s, NULL))     AS max_played_gap_s,
  MAX(s.gap_s)                                      AS max_idle_s,
  COUNT(DISTINCT EXTRACT(HOUR FROM s.game_time))    AS hours_of_day_covered,
  -- 24-dim hour-of-day activity profile: what the coordinated-cohort detector
  -- correlates pairwise. Hourly grain is what aggregates support; minute-grain
  -- correlation would need a dedicated BigQuery-side job over raw rounds.
  [COUNTIF(EXTRACT(HOUR FROM s.game_time) =  0),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) =  1),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) =  2),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) =  3),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) =  4),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) =  5),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) =  6),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) =  7),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) =  8),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) =  9),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) = 10),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) = 11),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) = 12),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) = 13),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) = 14),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) = 15),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) = 16),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) = 17),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) = 18),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) = 19),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) = 20),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) = 21),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) = 22),
   COUNTIF(EXTRACT(HOUR FROM s.game_time) = 23)]     AS hour_profile,
  COUNT(DISTINCT DATE(s.game_time))                 AS active_days,
  ANY_VALUE(p.max_rounds_per_minute)                AS max_rounds_per_minute,
  ANY_VALUE(p.superhuman_minutes)                   AS superhuman_minutes,
  ANY_VALUE(h.hot_small_sessions)                   AS hot_small_sessions,
  ANY_VALUE(h.sessions)                             AS sessions
FROM sess s
LEFT JOIN modal      m USING (parent, uid)
LEFT JOIN per_minute p USING (parent, uid)
LEFT JOIN hot        h USING (parent, uid)
GROUP BY s.parent, s.uid
HAVING rounds >= @min_rounds
