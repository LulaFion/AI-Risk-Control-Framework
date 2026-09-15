# riskdet — AI Risk Detector (GCP)

Four-layer risk-control pipeline over `acp-develop.OMG.RecordSlot` (~483M slot
rounds) with Cloud Logging evidence from `acp-prod`. Spec: [CLAUDE.md](CLAUDE.md).
Layers 1–3 are Python (`riskdet/`); Layer 4 is the Claude Code subagent team in
[.claude/agents/](.claude/agents/) — schema contract in
[docs/AGENT_CONTRACT.md](docs/AGENT_CONTRACT.md).

**Nothing here decides anything.** A signal is a reason to investigate;
findings exist only after investigation and skeptic review; enforcement is
100% human.

```
BigQuery (archive, replay-gated)          Cloud Logging (acp-prod, 30d)
        │                                          │
  Layer 1 rules ──┐                                │
  Layer 2 drift ──┼─ fuse (family independence) ─ emit ─┐   │
  Layer 3 cohort/ML ┘                                    │   │
        state gate (open/escalate/reopen, dedup) ◄───────┘   │
                    └─→ alerts_<scan>.jsonl · case_state.json │
                    └─→ output/case_queue.md · risk_rules_findings.csv
        Layer 4 agents: investigator → skeptic → report-composer
                                    └── evidence: out/logs/ (redacted)
```

## Data architecture

- **One paid scan, ever, of the unpartitioned source** (measured: a 30-day
  filter prunes zero bytes there). `05_bootstrap_archive.sql` materialised the
  full May–August history once (**$0.67, done 2026-08-18**, 482,840,721
  rounds) into `acp-develop.OMG_riskdet.rounds_all`, partitioned by UTC event
  date, clustered `(parent, uid)`. September+ arrives via the idempotent
  MERGE (`06_ingest_increment.sql`) — history is never rewritten.
- **Every analytical query reads through `rounds_asof(@as_of_time)`** — a
  table function whose argument is structurally mandatory. That is the
  future-data-leakage gate, and it makes August replayable in hourly/daily
  ticks at partition-pruned cost (a May-only pass scans 3.3 GiB vs the
  source's un-prunable 109 GiB).
- **Two clocks, never confused**: `game_time` = UTC event time (1-second
  resolution; partitioning, replay and leakage control). `ReportDate` = UTC+8
  business date (reporting only — it spans two UTC days and is never a
  boundary).
- **Raw rounds never leave BigQuery.** Only per-player/per-cell aggregates
  download (parquet in `out/`).

## Statistical protocol

- **Calibrate on May → FREEZE → validate on June–July → August hidden** for
  as-of replay. Thresholds are never fitted to the population being scanned.
- **No fixed guessed thresholds.** ROBUST metrics use median + N·MAD with the
  documented MAD-collapse fallback; rate metrics use their analytic binomial
  null; money uses certified RTP with the unequal-stake SE
  (σ_spin·√Σvalid_bet²); PRESCIENT_BET uses an exact permutation null from
  sufficient statistics. All Z rules pass Benjamini–Hochberg FDR **plus** an
  effect floor **plus** a volume floor.
- **The volume floor is for STATISTICAL rules only.** Absolute integrity breaches
  (`MAX_X_BREACH`, `BALANCE_IDENTITY`, `DUP_ROUND`) run on an **ungated**
  player-cell frame — true on a single round — so a low-volume breach is never
  dropped; the scan extracts ungated (post-agg `HAVING`, no extra bytes) and
  re-applies the floor in-memory for the statistical rules. The
  `risk-rule-designer` skill classifies any new rule (absolute vs statistical vs
  sub-floor-watch) and picks the floor accordingly — *"under honest play, could
  this be true by luck?"*
- **`alert_capacity_ceiling` is a ceiling, not a quota** — clean data
  producing zero findings is a PASS, verified by synthetic test.
- **Peer cell = `(game_id, play_type, currency, sm_tag)`.** Certified RTP is
  per playway (`OMG Game Info.csv`, Big5/cp950; Feature Buys cost 32.5–300×
  base), money never crosses currencies, baselines are
  leave-player-AND-operator-out.
- **Independence by family, not rule** (TIMING counts once however many of
  its rules fire; ENVIRONMENT never counts toward a player). Escalation:
  monitor → enhanced → human_review needs ≥2 families, or a quantitatively
  defined single-family route: the **statistical** route (leave-cohort-out σ≥6 +
  effect floor + both-halves persistence + certified baseline), or the
  **integrity** route (a single absolute breach — `MAX_X_BREACH`,
  `BALANCE_IDENTITY`, `DUP_ROUND` — escalates on its own, being a deterministic
  identity/contract violation with no variance false-positive risk).
- **Anti-selection-bias**: every candidate reports the expected null maximum
  z for the population tested; split-half persistence; the Coordinated
  Cohort Detector proposes (never excludes) account groups that move
  together, with competing explanations — exclusion requires human approval
  in `config/exclusions.yaml`, and unapproved cohorts get dual
  (inclusive + leave-cohort-out) reporting, never suppression.

## Case state & alerting

A scan is **stateless** — it re-emits every standing candidate — so run daily a
case would re-appear every day (hourly → 24×, per-minute → ~1,440×).
[`riskdet/casestore.py`](riskdet/casestore.py) is a persistent, cadence-agnostic
gate between fusion and the human queue that decouples **scan cadence** (how
often we look → fast detection) from **alert cadence** (state changes → what a
human sees). It opens a case once, re-alerts only on a *material change* (higher
escalation tier or severity, or a signal **family** that never fired before) or a
recurrence, and suppresses unchanged repeats — suppressed cases stay in the audit
trail, never hidden. It **respects recorded human dispositions** (a dismissed case
does not nag until something materially new appears) and **never enforces** — it
de-duplicates alerts only. Two knobs adapt it to any cadence: `min_persistence`
(debounce one-scan blips) and `close_after_absent` (anti-flap hysteresis). On the
week-1 replay it collapses **13,396 daily candidate-rows to 1,651 alerts (8.1×)**;
`candidates_<scan>.jsonl` remains the full cumulative audit queue, `alerts_<scan>.jsonl`
is the de-duplicated queue a human works.

A case that reaches `human_review` **auto-enters the Layer-4 queue** (once, gated
by the same state store) for `player-investigator → skeptic`; `monitor`/`enhanced`
cases stay as context.

The dashboard read-layer additionally **rolls up distributed integrity breaches**
into platform-defect cases (the same absolute breach across ≥2 accounts on one
game-cell, or across ≥2 cells/operators on one build → one case with per-account
drill-down), so a build-wide defect reads as one finding, not many.

## Operations

```bash
python -m riskdet check                       # offline readiness, free
RISKDET_DRY_RUN_ONLY=1 python -m riskdet run --as-of 2026-08-17T16:00:00
                                              # cost preview, executes nothing
python -m riskdet calibrate                   # fit May, validate Jun-Jul (paid ~$1)
python -m riskdet run --as-of 2026-08-17T16:00:00   # scan (paid ~$0.5)
python -m riskdet increment                   # monthly MERGE of new rounds
python -m riskdet pull-logs --case-id P_x_y_z --evidence "gsid@2026-08-10T12:00:00Z"
```

Every query is cost-guarded: free dry-run first, per-query byte cap,
cumulative run budget, a `measured_bytes` regression brake per SQL file, and
an audit ledger at `output/data_quality/query_cost_ledger.csv`. The bootstrap
refuses to run twice.

Credentials: `airc-740@acp-prod` (key at
`~/.config/gcloud/keys/`, outside the tree) serves both BigQuery and Cloud
Logging. The ambient ADC cannot read acp-prod logs; `check` flags it.
Evidence files are JWT/base64-redacted with a fail-closed `eyJ` gate —
verified against live spin-server payloads.

## Status

| Piece | State |
|---|---|
| Archive + replay gate + MERGE ingestion | **Live**, bootstrap done ($0.67) |
| Cost guard / ledger / leakage gate | Verified against BigQuery (free probes) |
| Catalog loader (cp950, per-playway certified RTP) | Verified on the real sheet |
| Layers 1–3 + fusion + emit | Verified on synthetic populations (clean → 0 findings; all planted attacks caught) |
| Case-state gate (dedup / alert cadence) | Verified (10 unit tests); wired into the replay, 8.1× alert reduction |
| Integrity single-family route + L4 auto-routing | Verified (5 unit tests); lone absolute breach → human_review → auto-enters Layer 4 |
| Platform-defect rollup (game-cell + build-wide) | Dashboard read-layer; distributed breaches collapse to one case, per-account drill-down |
| Evidence puller + redaction | Verified on a live production round |
| Calibration (`python -m riskdet calibrate`) | **Not yet run** — first paid calibration pending approval |
| Cloud Run job | Dockerfile ready; deploy blocked on IAM (see Dockerfile header) |

Known limits (by design, stated in reports): sub-second timing, reel windows,
double-settle-vs-ETL, wallet provenance, autoplay confirmation and
`resCode` retry patterns exist **only** in Cloud Logging (30-day retention) —
BigQuery candidates older than that are labelled permanently un-investigable.
Games without certified RTP (1058s, 6801r, 6901g, 1003m) are carried as
`baseline_availability='none'`, never silently dropped.
