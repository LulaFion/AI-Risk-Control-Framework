---
name: time-series-analysis
description: Risk time series monitoring for slot game fraud and exploit detection. Use when continuously monitoring player RTP, game math, and platform metrics over time to detect abnormal behavioral patterns. NOT a forecasting skill.
---

# Risk Time Series Analysis

> This skill is not for forecasting. It continuously monitors player, game, and platform metrics over time to detect persistent shifts, drifts, and spikes that may indicate fraud, exploits, game math bugs, or structured manipulation.

# When to use
- A player has triggered multiple risk alarms and you need to determine whether their elevated RTP is a single spike or a persistent trend
- A game's aggregate RTP has been drifting upward and you need to locate the change point and quantify the shift
- The daily alarm count is increasing week-over-week and you need to determine whether this is a genuine trend or noise
- A Feature RTP spike appeared on a specific date and you need to identify which players and game states are responsible
- You need to compare a player's current behavioral trend against historical exploit cases to assess similarity

# Process
1. **Extract the time series** — build per-entity daily metric sequences from `spin_history.csv` or `rawdata.csv`. Scope to the analysis window (7-day default for players, 30-day for games). Compute `DailyRTP`, `DailyNW`, `FeatureBuyRate`, `AlarmCount`, and other metrics listed in `references/ts_patterns_guide.md`.
2. **Compute rolling baseline** — calculate a rolling mean and standard deviation over the pre-anomaly window to establish the expected behavior baseline (`μ_0`, `σ`). For game-level metrics, use the theoretical RTP as `μ_0` when available.
3. **Apply CUSUM for persistent shift detection** — run an upward CUSUM (`C_t^+ = max(0, C_{t-1}^+ + (x_t − μ_0 − k))`) on player `DailyRTP` and `NW_Diff`. A breach of the decision threshold `h` indicates a sustained mean shift, not a one-off spike. Default parameters: `k = 0.05`, `h = 5`. See `references/ts_patterns_guide.md` for parameter guidance.
4. **Apply EWMA for trend monitoring** — fit an EWMA (`α = 0.2`) to smooth the metric series and compute control limits (UCL/LCL). A breach of the UCL that persists across multiple days confirms a gradual upward drift. Use EWMA for `AlarmCount`, `HighRTPPlayerCount`, and game-level `BonusTriggerRate`.
5. **Apply Z-score and MAD for spike detection** — flag individual days where `|z| > 3` (Z-score) or the modified Z-score exceeds 3.5 (MAD). Spikes that coincide with CUSUM accumulation are more significant than isolated spikes. Use MAD for skewed metrics such as `BetAmount` and `WinAmount`.
6. **Locate change points** — for retrospective investigations ("when did this start?"), run change point detection on the metric series using `scripts/ts_analyzer.py --changepoint`. Identify the exact date of structural break and compute the pre/post-break mean difference.
7. **Classify the risk pattern** — match the detected signal against named patterns in `references/ts_patterns_guide.md`: Continuous High RTP, Continuous Profit, Sudden RTP Increase, Feature RTP Spike, Bonus Frequency Spike, NW_Diff Trending Up, Alarm Count Rising, Game RTP Shift.
8. **Compare against historical cases** — for each detected anomaly, compute similarity against historical exploit cases across five dimensions: RTP trajectory shape, feature state concentration, persistence duration, entity scope, and statistical test results. Surface the top-3 matching cases with similarity scores.
9. **Generate AI explanation** — for each confirmed anomaly produce a structured finding: what happened, why it is abnormal, possible causes, confidence level, triggered risk rules, matching historical cases, and suggested investigation steps. Use the format in `assets/ts_report_template.md`.
10. **Output the risk report** — write the executive summary, detected trends table, and per-entity investigation recommendations. Escalate entities with Risk Score ≥ 80 to the manual review queue. Continue enhanced monitoring (daily CUSUM scan for 14 days) for entities scoring 60–79.

# Inputs the skill needs
- Player or game daily metric sequences: `DailyRTP`, `DailyNW`, `NW_Diff`, `FeatureBuyRate`, `AlarmCount` (minimum 7 days for player-level, 14 days for game-level)
- Baseline values: theoretical RTP per game, site-level average RTP, or prior-period mean
- Game metadata from `_GameList.csv` to filter SLOT games and look up theoretical RTP
- Risk rule trigger log or derived alarm counts from `RiskRuleLibrary.md` thresholds
- Historical exploit case library for similarity comparison (Case IDs, feature vectors, exploit type)

# Output
- `scripts/ts_analyzer.py` — computes CUSUM, EWMA, Z-score, MAD, and change point detection per entity; outputs `cusum_signals.csv`, `ewma_chart_data.csv`, and `detected_trends.md`
- `references/ts_patterns_guide.md` — named risk patterns, CUSUM/EWMA parameter guidance, method-to-metric recommendation matrix, and common pitfalls
- `assets/ts_report_template.md` — report template: executive summary, detected trends table, per-entity AI explanation, historical case matches, investigation recommendations, and escalation decisions
