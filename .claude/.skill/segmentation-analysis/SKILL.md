---
name: segmentation-analysis
description: Player behavioral segmentation and risk profiling for slot game risk control. Use when classifying players into risk segments, generating individual risk profiles, comparing behavior against historical exploit cases, or routing players to the correct investigation path.
---

# Player Segmentation & Profiling

> This skill classifies players into behavioral segments based on gameplay patterns, mathematical performance, and historical risk signatures. It supports Risk Control, Fraud Detection, Player Investigation, AI Recommendation, and Historical Case Comparison.

# When to use
- A player has triggered multiple risk rules and needs full behavioral context before manual review
- The risk team needs a daily triage list — which players warrant attention today and why
- A new game shows abnormal aggregate RTP and the contributing player segments need to be identified
- An analyst needs a structured profile with segment label, risk score, and next-action recommendation before opening an investigation
- Detected time series anomalies (from the `time-series-analysis` skill) need to be matched against known exploit archetypes

# Process
1. **Load and filter** — read `spin_history.csv` or `rawdata.csv`, filter to SLOT games only (`GameKind = SLOT` from `_GameList.csv`), and scope to the investigation window (7-day default, 30-day for persistence checks). Preserve grain `(UserId, WebSite, GameType, Date, IsPro)` until aggregation is intentional.
2. **Engineer raw features** — compute per-player metrics aggregated over the window. Betting Behavior: `AvgBet`, `BetVariance`, `BetCV`, `MaxBet`, `BetIncreasePattern`. Winning Performance: `OverallRTP`, `DailyRTP`, `FeatureRTP`, `BonusRTP`, `WinRate`, `ConsecutiveProfitDays`. Session Behavior: `AvgSpinsPerSession`, `SpinsPerMinute`, `PlayTimeDistribution`, `NightPlayRatio`. Feature Usage: `FeatureBuyRate`, `BonusEntryRate`, `AutoSpinRate`. Risk Indicators: `NW`, `NW_Diff`, `AlarmCount`, `RiskRulesTriggered`. See `references/segmentation_approaches.md` for full feature definitions and formulas.
3. **Engineer derived scores** — compute higher-level behavioral scores that combine raw features into interpretable risk dimensions: `ProfitStability` (1 − CV of DailyNW), `FeatureDependency` (FeatureBuyRate × FeatureRTP/OverallRTP), `RTPStability` (1 − CV of DailyRTP), `BotScore` (SessionConsistency × SpinsPerMinute / 10), `RiskPersistence` (ConsecutiveProfitDays / ActiveDays), `BonusReliance`, `BetTimingScore` (Spearman corr of BetAmount vs SpinIndex), and `GameConcentration` (Herfindahl index over games played).
4. **Apply rule-based classification first** — assign priority segment labels directly from threshold rules before clustering: Bot (`SpinsPerMinute > 10` AND `BetCV < 0.05`), Advantage Player (`OverallRTP > 1.20` for ≥ 4 days AND `RiskPersistence > 0.7`), Fraud Group (`MultiAccountScore > threshold`), Feature Buy Player (`FeatureBuyRate > 0.50` AND `FeatureDependency > 1.5`), Bonus Hunter (`BonusEntryRate` high AND `BonusRTP >> BaseRTP`). Rule-based labels are auditable and directly traceable to `RiskRuleLibrary.md`.
5. **Cluster the unlabeled residual** — for players not assigned a rule-based label, run Isolation Forest to flag anomalies, then apply HDBSCAN to cluster the flagged pool into behavioral sub-groups. Validate that clusters are meaningfully distinct using silhouette score (> 0.3) and assign descriptive segment names. Remaining unlabeled players default to Normal Player, Casual Player, Whale, High Roller, VIP Player, or New Player based on `AvgBet`, `ActiveDays`, and `AccountAge`. See `references/segmentation_approaches.md` for clustering method comparison and parameter guidance.
6. **Compute risk score and confidence** — calculate `RiskScore` (0–100) as a weighted combination: RTP deviation (25%), profit persistence (20%), bot signals (20%), feature dependency (15%), multi-account linkage (10%), alarm density (10%). Compute `ConfidenceScore` and apply penalties: cap at 40% if `ActiveDays < 3`, cap at 50% if `TotalSpins < 50`, reduce by 15% if device or IP data is missing.
7. **Compare against historical cases** — for each player with `RiskScore ≥ 60`, compute weighted similarity against historical exploit cases across five dimensions: RTP trajectory (35%), behavioral pattern — FeatureBuyRate, BonusReliance, BetCV (30%), risk pattern — triggered rules overlap (20%), session behavior (15%). Return the Top 10 most similar cases with similarity scores and inferred root cause. Use `scripts/segmentation_runner.py --historical-match`.
8. **Generate the player risk profile** — for each flagged player produce a structured profile: `PlayerType`, `RiskScore`, `ConfidenceScore`, `KeyCharacteristics` (top 3–5 driving features), `TriggeredRiskRules`, `HistoricalSimilarity` (top match), and `AIExplanation` (plain-language narrative). Use the format in `assets/segment_profile_template.md`.
9. **Route to investigation** — map each segment to its recommended next investigation step: Advantage Player → run Monte Carlo simulation and compare with historical exploit cases; Bot → analyze inter-spin timing distribution and review DeviceId/IP linkage; Fraud Group → cross-account analysis and IP clustering; Feature Buy Player → pull Feature Buy event logs and verify game-state transitions; Bonus Hunter → audit bonus trigger frequency against theoretical rate. Escalate players with `RiskScore ≥ 80` to the manual review queue immediately.
10. **Output the segment report** — write the segment summary, top-risk player list, and per-player investigation recommendations. Flag any clusters where multiple players share the same `GameType`, `WebSite`, and elevated `FeatureBuyRate` — this combination indicates possible coordinated exploitation.

# Inputs the skill needs
- Player spin or daily aggregate data covering the analysis window (minimum 7 active days recommended; 3-day minimum for initial triage)
- Game metadata from `_GameList.csv` to filter SLOT games and provide brand and game name context
- Risk rule trigger log or alarm counts derived from `RiskRuleLibrary.md` thresholds
- Historical exploit case library with feature vectors per case (Case ID, exploit type, key metrics, outcome)
- Device and IP linkage data for multi-account and fraud group detection (optional but required for high-confidence Bot and Fraud Group labels)

# Output
- `scripts/segmentation_runner.py` — computes raw and derived features, applies rule-based classification, runs Isolation Forest and HDBSCAN, generates risk scores, and performs historical case matching; outputs `player_risk_profiles.csv` and `historical_matches.csv`
- `references/segmentation_approaches.md` — full feature definitions and formulas, clustering method comparison (rule-based vs K-Means vs DBSCAN vs HDBSCAN vs Isolation Forest vs Autoencoder vs LLM-assisted), segment characteristics table, and risk score weight documentation
- `assets/segment_profile_template.md` — per-player profile template covering PlayerType, RiskScore, ConfidenceScore, KeyCharacteristics, TriggeredRules, HistoricalSimilarity, AIExplanation, and recommended investigation actions
