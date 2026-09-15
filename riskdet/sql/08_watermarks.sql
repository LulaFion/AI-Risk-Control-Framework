-- description: ingestion watermarks off the archive. Cheap: two columns of the
--              partitioned table. Feeds increment()'s @serial_watermark and
--              @modified_since. No as_of gate here on purpose -- watermarks
--              must see the true physical frontier, not the replay boundary.
SELECT
  MAX(serial)    AS max_serial,
  MAX(game_time) AS max_game_time
FROM {{rounds_table}}
