# Programmatic EDA Skill Overview

This skill facilitates systematic exploratory data analysis by automating dataset profiling workflows. It activates when you need to assess data structure, quality, and reliability before conducting deeper analysis.

## Key Activation Scenarios

The skill engages when you: receive a new dataset requiring structural understanding, encounter surprising analytical results warranting verification, respond to stakeholder questions about data trustworthiness, or prepare for modeling work requiring quality assurance.

## Core Workflow

The process follows seven sequential steps:

1. **Initial Assessment** — Execute data overview scripts to determine row counts, data types, memory consumption, and samples while confirming what each row represents.

2. **Null Value Analysis** — Profile missing data and cross-reference findings against documented quality benchmarks, flagging problematic columns.

3. **Anomaly Identification** — Apply statistical detection methods (IQR and z-score techniques) to numeric columns, then evaluate whether flagged values represent genuine patterns or data errors.

4. **Statistical Characterization** — Generate descriptive statistics and visual distributions for all numeric fields.

5. **Relationship Mapping** — Examine variable correlations, identifying pairs exceeding 0.8 thresholds as potential redundancy concerns.

6. **Verification Checklist** — Complete structured validation documentation before formally concluding the profiling phase.

7. **Documentation** — Synthesize findings into comprehensive reports highlighting primary data quality concerns and recommended actions.

## Required & Optional Inputs

Essential inputs include dataset location and business context defining row-level meaning. Optional parameters encompass threshold customization and column exclusion lists for sensitive or irrelevant fields.

## Deliverables

The skill produces comprehensive profiling reports and executive summaries documenting major quality issues with actionable recommendations.

---

## Scripts

| Script | Purpose | Key CLI flags |
|---|---|---|
| `scripts/data_overview.py` | Shape, dtypes, memory, nulls, sample rows | `--input` `--sample` |
| `scripts/null_profiler.py` | Null % per column with WARN/FAIL status | `--input` `--warn-pct` `--fail-pct` `--output` |
| `scripts/outlier_detector.py` | IQR and z-score outlier detection | `--input` `--method` `--iqr-k` `--z-thresh` `--output` |
| `scripts/distribution_summary.py` | Descriptive stats + ASCII histograms | `--input` `--bins` `--output` |
| `scripts/correlation_explorer.py` | Pairwise correlation matrix + strong pairs | `--input` `--threshold` `--method` `--output` |

## Quick Start

```bash
python scripts/data_overview.py --input <your_file>.csv
python scripts/null_profiler.py --input <your_file>.csv
python scripts/outlier_detector.py --input <your_file>.csv --method both
python scripts/distribution_summary.py --input <your_file>.csv
python scripts/correlation_explorer.py --input <your_file>.csv --threshold 0.8
```
