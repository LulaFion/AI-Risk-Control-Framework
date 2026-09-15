# Agent contract — schema mapping and artifact surface

The five subagents in `.claude/agents/` were originally written for the
`Risk Detection/` sandbox workspace and have been ported to this GCP pipeline.
This document is the single reference for what maps to what, so no agent (or
human) reaches for a file or a column that does not exist here.

## Schema mapping (sandbox -> GCP)

| Sandbox term | GCP term | Notes |
|---|---|---|
| `WebSite` | `parent` | the operator; `uid` is scoped to it |
| `GameType` (agents) | `game_id` | e.g. `1049s` — the game's identity |
| `UserId` | `uid` | never linked across parents |
| `CIn` | `valid_bet` | the RTP denominator (certified RTP is defined on valid turnover) |
| `Nw` | `netWin` = `win - bet` | exact identity, verified; quote as-is |
| `GameKind == SLOT` | all of `OMG.RecordSlot` | non-slot round games are scope-excluded in `config/exclusions.yaml` |
| `spin_history.csv` | `acp-develop.OMG_riskdet.rounds_all` | raw rounds stay in BigQuery; only aggregates download |
| `session_id` | (none) | sessions are synthesised from 30-minute idle gaps |
| `outcome_x` (board) | Cloud Logging `slotWindow` only | not in BigQuery at all |

## Three things wear the name "GameType" — never confuse them

1. `RecordSlot.game_type` — INT64 constant `70` (product line). Useless.
2. The agents' old `GameType` — the game identity → **`game_id`**.
3. `OMG Game Info.csv` column `GameType` — the playway → **`play_type`**
   (0=Base, 1=Extra Bet/SureWin, 2/3/4=Feature Buy tiers at 32.5–300× base).

## Files the old agents referenced that DO NOT exist here

`risk_rules.py`, `auto_analysis.py`, `DataDictionary.md`, `RiskGlossary.md`,
`RiskRuleLibrary.md`, `Workflow.md`, `risk_sandbox.db`, answer keys.
Replacements: the `riskdet` package (detection), `config/thresholds.yaml`
(rule definitions incl. the `retired:` section), `docs/AGENT_CONTRACT.md`
(this file), `OMG Game Info.csv` (certified RTP + max multiplier per
game/playway). This is production data — every "synthetic data" caveat from
the sandbox is void, including the small-decimal-credit rescaling stage.

## Artifact surface

| Path | Written by | Read by |
|---|---|---|
| `output/case_queue.md` | Python (`riskdet.merge.emit`) | investigator, composer |
| `output/risk_rules_findings.csv` | Python (carries `window_start/window_end` for staleness) | composer |
| `output/data_quality/*.md` (literal `Verdict:` line) + `query_cost_ledger.csv` | Python | composer (caveats first) |
| `out/candidates.jsonl` | Python | any agent needing full signal detail |
| `out/logs/<case_id>/<gsid>.json` + `out/logs/_index.csv` | Python (`riskdet.cloudlogs`) — JWT/base64-redacted | investigator, skeptic |
| `out/proposed_exclusions.yaml` | Python (cohort detector) | skeptic (competing explanations), humans (approval) |
| `output/cases/<case_id>.md` | investigator / query-analyst; skeptic appends | composer |
| `output/reports/daily_digest_<start>_<end>.md` | composer | humans |

Case ids: `P_<parent>_<game_id>_<uid>` (player; `ALL` in the game slot for
player-wide cases) and `G_<game_id>_pt<play_type>_<sm_tag>_<date>`
(game-level). Components are sanitised to `[A-Za-z0-9_.-]`.

## Invariants every agent must hold

- RTP = `SUM(win)/SUM(valid_bet)`; exclude `valid_bet <= 0` and say so.
- Never sum raw money across currencies (MMK↔USD ≈ 1e5 scale spread).
- Peer cell = `(game_id, play_type, currency, sm_tag)`; never widen across
  `play_type`.
- `game_time` = UTC event time, 1-second resolution; `ReportDate` = UTC+8
  business date; state which clock every quoted date uses; `ReportDate` is
  never a replay boundary.
- Every BigQuery read goes through `riskdet`'s cost guard and the
  `rounds_asof(@as_of_time)` leakage gate.
- Log evidence exists for 30 days only; older rounds are permanently
  un-investigable and must be reported as such, not as absence of evidence.
- Cohort membership (`cohort_id` on a case) is context, not evidence:
  report inclusive AND leave-cohort-out comparisons; exclusion requires a
  human-approved entry in `config/exclusions.yaml`.
- A signal is a reason to investigate. Findings exist only after
  investigation and skeptic review. Nothing in this pipeline decides.
