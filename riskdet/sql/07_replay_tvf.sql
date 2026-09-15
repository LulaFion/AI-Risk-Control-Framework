-- description: THE leakage gate. Creates the table function every downstream
--              query reads rounds through -- never the archive directly. The
--              as_of parameter is structurally mandatory (a TVF cannot be
--              called without its argument), so "forgot the boundary" is not
--              an expressible mistake. Calling it with successive as_of values
--              replays August (or any period) as if data were arriving hourly
--              or daily, with zero rescans of the unpartitioned source.
--
--              Boundary is on game_time -- UTC EVENT time. The predicate is on
--              the partitioning column's date, so BigQuery prunes partitions:
--              a May replay reads May partitions only.
--
--              ReportDate (UTC+8 business date) is exposed for grouping but is
--              NOT the boundary: one business date spans two UTC dates, so
--              bounding on it would leak up to 16 hours of future events.

CREATE OR REPLACE TABLE FUNCTION {{dataset}}.rounds_asof(as_of DATETIME)
AS
SELECT *
FROM {{rounds_table}}
WHERE game_time <= as_of
