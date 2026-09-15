-- description: pre-injection guard -- do any stealth uids already exist under
--              their target operators? Must return 0 rows before MERGE, else a
--              synthetic uid would pollute a real account's stats.
SELECT parent, uid, COUNT(*) AS n
FROM {{rounds_table}}
WHERE parent IN UNNEST(@parents) AND uid IN UNNEST(@uids)
GROUP BY parent, uid
