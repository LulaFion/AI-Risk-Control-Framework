-- measured_bytes: 95000000000
-- description: Build the OMG_riskdet_w1 rounds table as a COPY of the real
--              archive (which already holds May-Aug real data). Injected fake
--              rows are appended by a separate load job (free), never here.
--              Partitioned/clustered identically so rounds_asof prunes the same.
CREATE OR REPLACE TABLE {{w1_table}}
PARTITION BY DATE(game_time)
CLUSTER BY parent, uid
OPTIONS (description = 'week-1 replay copy of OMG_riskdet.rounds_all + injected '
                       'synthetic rounds (serial >= 9.1e9). Rebuildable; DELETE '
                       'serial >= 9100000000 to remove injects.')
AS SELECT * FROM {{rounds_table}}
