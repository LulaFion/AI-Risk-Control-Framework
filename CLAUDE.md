# CLAUDE.md

## Project: AI Risk Detector (GCP Environment)

### Core Concept

This project does not rely on rigid, fixed risk rules.

Traditional risk control often uses fixed thresholds, such as treating every player
with RTP above 130% as suspicious. These thresholds can wrongly flag legitimate
players who were simply lucky, played at low volume, or won a rare jackpot.

Instead, the AI acts as a **private detective with statistical expertise**. It can:

- Proactively scan new data for unusual behavior across GCP BigQuery tables and Cloud Logging.
- Investigate questions raised by human risk-control experts.
- Design an appropriate analytical method for each case.
- Write and execute SQL against BigQuery (`acp-develop.OMG.RecordSlot`) or Python scripts against Cloud Logging (`acp-prod`).
- Combine behavioral, statistical, time-series (BQML ARIMA_PLUS), and ML evidence (targeted Monte Carlo simulation is planned — see Layer 4).
- Challenge its own conclusions and report alternative explanations.

The AI is not limited to a fixed rule library or a human request as its starting
point. It may begin from a human question, a scheduled scan, a newly detected
anomaly, or evidence discovered during another investigation.

## Decision Boundary

The AI assists decision-making; it never imposes judgments.

Its responsibilities are limited to:

- Finding and validating evidence across BigQuery records and Cloud Logging payloads.
- Measuring statistical significance and practical effect.
- Estimating probabilities and uncertainty.
- Testing competing explanations (e.g. distinguishing QA/load-test bot fleets or autoplay from real exploits).
- Identifying evidence that would disconfirm a finding.
- Recommending monitoring, enhanced monitoring, or human review.

The AI must never declare fraud, punish a player, restrict an account, or make an
enforcement decision. Final decision-making authority remains **100% with human
risk-control experts**.

## GCP Data Architecture & Constraints

### Two-Stage Architecture
1. **Stage 1 — BigQuery Screening (`acp-develop.OMG.RecordSlot`):**
   - Coarse screening across 30-day lookback windows (`window_days`).
   - Outputs candidates as `game_seq_id@timestamp` for precise round-level evidence lookup.
   - Cost guarded via dry runs and `--maximum_bytes_billed` limits in [scripts/run_detection.ps1](scripts/run_detection.ps1).
2. **Stage 2 — Cloud Logging Evidence (`acp-prod` Pod Logs):**
   - Pulls spin-server (`slotWindow`, `beforeBalance`/`afterBalance`, `mathVer`, `requestId`, `resCode`) and pod logs via [scripts/pull_logs.py](scripts/pull_logs.py).
   - Constrained by `_Default` log bucket **30-day retention**.

### Key GCP Schema & Data Realities
- **GCP Table Structure:** `RecordSlot` (~196 GB) is CLUSTERED on `(serial, game_time)` but NOT date-partitioned. Column pruning is essential.
- **Timestamp Resolution:** BigQuery `game_time` has 1-second resolution (UTC). Sub-second spin gaps and exact millisecond durations exist only in Cloud Logging (`duration`).
- **Data Types & Enums:** `test_demo_play`, `system_take_win`, `wallet_mode` are STRING (`'True'`/`'False'`). Demo traffic (`parent='acdemo'`, `test_demo_play='True'`) is excluded everywhere.
- **QA / Test Fleet Contamination:** Test accounts (e.g., sequential UIDs `x2n0000000NN` under `v3cnys_*` parents) must be filtered out to prevent statistical tail selection bias.
- **ReportDate Timezone:** `ReportDate` is a **UTC+8 business date**, whereas `game_time` is **UTC**.

## Four-Layer Risk-Control Architecture

### Layer 1 — Explainable Rule-Based First Scan

Run fast, transparent rules for obvious abnormalities:

- RTP and net-win outliers, adjusted for sample size and turnover.
- Feature Buy and bonus-trigger ratios.
- Betting cadence and same-second bursts.
- Wallet balance discontinuities.
- Duplicate rounds and concurrent play.

Rules must compare players within an appropriate peer group such as game,
currency, math build (`sm_tag`), and activity level. Thresholds are configurable
and may use robust population baselines such as Median + 12·MAD with a documented
MAD-collapse fallback.

A rule match creates an investigation candidate; it is not evidence of fraud.

**Rule class decides the round floor** (formalised by the `risk-rule-designer`
skill). Ask: *under honest play, could this be true by luck?*

- **No** — a contract/identity/cap violation (`MAX_X_BREACH`, `BALANCE_IDENTITY`,
  `DUP_ROUND`) is ABSOLUTE. It runs on the **ungated** player-cell frame
  (`integrity_pc`), is true on a single round, and uses the `ABSOLUTE` threshold
  method. It is **not** subject to the `min_rounds` floor — the scan pulls the
  frame ungated (a post-aggregation `HAVING`, so no extra bytes) and re-applies
  the floor in-memory for statistics only. Gating integrity out was a production
  miss, now fixed.
- **Yes** — a population/baseline comparison (RTP/turnover z, trigger-rate,
  drift) is STATISTICAL. It keeps the calibrated `min_rounds` floor plus BH-FDR
  plus split-half persistence, because small samples manufacture variance
  false-positives.

Never default a deterministic breach to the round floor; never let a statistical
rule fire floor-free.

### Layer 2 — Statistical, Time-Series Analysis

Use statistical or time-series methods appropriate to the available data.

Possible methods include Bayesian change-point detection, ARIMA, distribution tests, robust regression, or other validated approaches. These are reference options, not required methods.

The selected method must match the data grain, history length, sample size, and statistical assumptions. Time-series models require chronological validation.

### Layer 3 — Machine Learning and Anomaly Detection

Use ML to discover patterns not anticipated by explainable rules or statistical
tests.

Possible methods include Isolation Forest, Gaussian Mixture Models, autoencoders, clustering, or other suitable models. The AI may replace or omit them when the
data does not support their assumptions.

Unsupervised anomalies mean only "unusual." Models must be validated across game,
currency, math build, volume, and known test-account segments before their output
enters the candidate list.

### Layer 4 — AI Agent Team

After Layers 1–3 produce and merge the candidate list, a case that reaches
`human_review` **auto-enters the Layer-4 queue** (deterministic routing gated by
the case-state store, so a case enters once when it crosses the tier — not every
scan; a recurrence or new evidence after a disposition re-queues it). Routing is
a policy of the tier, never an agent decision. `monitor`/`enhanced` cases stay in
the queue as context and are not investigated by the agents.

- **Investigator (`player-investigator`):** joins BigQuery findings with round-level Cloud Logging evidence.
- **Targeted Monte Carlo (planned — not yet implemented):** the intended capability is, when a valid math-sheet or empirical baseline exists, to estimate the candidate's tail probability using 20,000+ simulations and report simulation uncertainty. It is reserved for *statistical magnitude* candidates with no closed-form null (e.g. a "won K of N raised-stake rounds" prediction-success case). It does **not** apply to absolute integrity breaches (deterministic — no variance to simulate) or to cases with an exact null (within-player permutation for prescient-bet, binomial for rate rules). No MC module is wired into the investigator today.
- **Cloud Logging review:** validate `slotWindow`, balances, math version, request path, timing, and response behavior within the 30-day retention window.
- **Skeptic (`skeptic`):** test benign alternatives, selection bias, QA fleets, autoplay, deposits, withdrawals, and data artifacts.
- **Report Composer (`report-composer`):** assemble already investigated and skeptic-reviewed findings for human review.

Layer 4 reports evidence and uncertainty. Final decisions remain entirely with human risk-control experts.

## Multi-Direction Fusion

Cross-reference evidence from multiple layers instead of relying on one score or method:

- Behavioral anomaly plus unusual winnings distribution.
- Statistical anomaly verified against Cloud Logging spin payload (and, once implemented, targeted Monte Carlo simulation — see Layer 4).
- ML anomaly explained by a known player cohort or test-account fleet (`v3cnys_*`).
- Game-level drift concentrated in one account (`CONCENTRATION`).
- High RTP without behavioral anomalies, treated as possible variance.

Escalation requires multiple independent signals or exceptionally strong, validated evidence. Correlated signals derived from the same underlying event must not be counted as independent confirmation.

**Single-family escalation routes.** Two quantitatively-defined exceptions let a
single family reach `human_review`, because an undefined "exceptionally strong"
clause would otherwise be used to escalate anything:

- **Exceptional statistical route** — a single *statistical* family qualifies only
  if it clears ALL of: leave-cohort-out σ ≥ 6, an effect floor, both-halves
  persistence, and a certified baseline.
- **Integrity route** — a single **absolute** integrity breach (`MAX_X_BREACH`,
  `BALANCE_IDENTITY`, `DUP_ROUND`) escalates on its own, because it is a
  deterministic identity/contract violation with no variance-driven
  false-positive risk. Integrity signals have no z and so can never use the
  statistical route; `DUP_ROUND` escalation means "a human/log must adjudicate
  double-settle vs ETL duplicate", not a confirmed finding.

**Concentration vs distribution.** A game-level anomaly concentrated in one
account is `CONCENTRATION`; the inverse — the **same** absolute breach appearing
across **multiple accounts** — is the fingerprint of a **game/platform-level
defect** (e.g. a wrong math sheet), not N independent players. Breaches are
therefore rolled up for human review at two grains, per scan day, with the
per-account cases always preserved for drill-down:

- **Game-cell grain** — the same breach on ≥2 accounts within one
  `(operator, game_id, play_type, sm_tag)` → one grouped case.
- **Build-wide grain** — the same breach on one **build (`sm_tag`)** spanning ≥2
  cells/operators → one build-wide/cross-operator case (fix the build at source).
  A single distributed defect (e.g. a wrong math build deployed to many operators)
  is ONE finding, not many.

## Investigation Workflow

1. Define the population, peer groups, time window, and investigation scope.
2. Exclude demo traffic and only those QA/load-test accounts confirmed by an
   approved exclusion list.
3. Run Layers 1, 2, and 3 independently against the eligible population.
4. Merge and deduplicate their findings into one candidate list, preserving each
   signal's source and evidence.
5. Use Layer 4 to investigate candidates with Cloud Logging and targeted Monte
   Carlo where an appropriate baseline exists.
6. Require Skeptic review before a risk-relevant finding reaches human review.
7. Use `report-composer` to assemble the verified evidence, uncertainty, disconfirming evidence, and recommended follow-up.

## Case State & Alert De-duplication

A scan is stateless: it re-emits every standing candidate every time it runs. Run
daily, a case would re-appear every day; run hourly or per-minute, tens to
thousands of times. A persistent, cadence-agnostic **case-state gate**
(`riskdet/casestore.py`) therefore sits between fusion and the human queue and
**decouples scan cadence from alert cadence**:

- **Scan often** (fast detection) but **alert only on state change** — a case is
  opened once, then re-alerts only on a *material change* (escalation tier or
  severity increases, or a signal **family** fires that the case never showed
  before) or a recurrence after closing. Unchanged repeats are suppressed.
- **Suppression is de-duplication, never concealment.** Every case remains in the
  persistent store with its full transition history; a suppressed case is still
  visible in the audit trail.
- **Recorded human dispositions are respected**: an acknowledged case does not
  re-alert until something materially new appears; the gate never acts on a
  disposition — enforcement remains 100% human.
- Cadence adapts via `min_persistence` (debounce one-scan blips) and
  `close_after_absent` (anti-flap hysteresis), so the same gate serves daily and
  sub-daily scans.

`candidates_<scan>.jsonl` is the full cumulative audit queue; `alerts_<scan>.jsonl`
is the de-duplicated queue a human works. This is temporal de-duplication across
scans, complementing the within-scan family de-duplication of step 4.

## Reporting Requirements

Every report must include:

- Scope, parent/operator, and population.
- Data sources (BigQuery datasets, Cloud Logging log types, date range, exclusions).
- Method and justification.
- Assumptions and validation checks.
- Results, effect sizes, and probabilities where applicable.
- Supporting and contradicting evidence (including Cloud Logging spin payload details).
- Most plausible cause and at least one benign alternative.
- Confidence level and caveats.
- A concrete way to disconfirm the conclusion.
- Recommended follow-up: monitor, strengthen monitoring, or human review.

Every conclusion must be evidence-based, uncertainty-aware, reproducible, and traceable to source data or a stated analytical method.