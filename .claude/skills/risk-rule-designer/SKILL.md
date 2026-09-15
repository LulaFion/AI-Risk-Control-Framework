---
name: risk-rule-designer
description: Design a new risk-detection rule for the riskdet pipeline (GCP/). Use whenever adding, proposing, or reviewing a new detection rule/signal — it classifies the rule as absolute / statistical / sub-floor-watch, decides whether it needs the min_rounds floor (a truth on 1 round vs variance needing volume), and specifies the threshold method, escalation route, code-integration points, and guardrails. Triggers on "add a rule", "new detection", "should this need >200 rounds", "design a signal", "when is a risk deterministically true".
---

# Risk-rule designer

Every new rule must be classified BEFORE it is written, because the class
decides the round floor, the threshold method, and how it may escalate. Skipping
this is how a rule ends up either flooding the queue (statistical rule with no
floor) or silently missing real breaches (absolute rule wrongly gated at 200).

## The one decision test

> **Under honest play, could this signal ever be true by luck (variance)?**

- **No** — it violates a contract, identity, or hard cap that is impossible under
  honest play → **ABSOLUTE**. No round floor; true on a single round.
- **Yes** — extreme values occur by chance, especially in small samples →
  **STATISTICAL**. Needs the calibrated floor + FDR + persistence.
- **Yes, but suggestive and cheap to watch below the floor** → **SUB-FLOOR
  WATCH**. Monitor-only, never escalates on its own.

## Classification → requirements

| Class | Examples | Round floor | Threshold method | Runs on | Escalation |
|---|---|---|---|---|---|
| **ABSOLUTE** (family `INTEGRITY`) | max_multiple > certified cap; `after ≠ before − bet + win`; same `game_seq_id` settled twice | **None** — true on 1 round | `ABSOLUTE` (sheet / identity) | **ungated `integrity_pc`** | may escalate **alone** via the integrity single-family route |
| **STATISTICAL** (`OUTCOME_MAGNITUDE/FREQUENCY`, `PRESCIENCE`, drift) | RTP / excess-turnover z, trigger-rate, hit-rate, L2 drift | **Calibrated `min_rounds_per_cell`** | robust-z / binomial / permutation, **+ BH-FDR + split-half persistence** | floor-gated `pc` | needs **≥2 families**, or the exceptional-statistical route (z≥6 + persistence + effect + certified) |
| **SUB-FLOOR WATCH** (non-contributing family, e.g. `MAGNITUDE_WATCH`) | material money at a rate far above certified but under the floor; trigger-rate watch (200–2000) | between `min_rounds` (≈10) and the floor | robust, deterministic | ungated / sub-floor slice | **monitor/watch only** — never contributes to the family count |

Absolute signals have **no z** and never use the statistical route. A rule that
compares to a *population baseline* is statistical even if it feels obvious —
"obvious" is not "impossible under honest play".

## Code-integration checklist (riskdet/)

1. **Family tag** — set the `Signal.family` to the class above. Fusion counts
   families, not rules; the tag is what makes escalation correct.
2. **Where it runs** ([layer1/rules.py](../../../riskdet/layer1/rules.py)):
   - ABSOLUTE → read from `ipc` (the ungated integrity frame). Confirm the caller
     passes `integrity_pc` ([pipeline/run.py](../../../riskdet/pipeline/run.py)
     passes the ungated frame; the w1 sim passes `pc_full`).
   - STATISTICAL → read from the floor-gated `pc`; require FDR + persistence.
3. **Config** ([config/thresholds.yaml](../../../config/thresholds.yaml)) — store
   MULTIPLIERS / target rates / quantile targets, never literal values. A literal
   number is allowed ONLY where physically/contractually meaningful (certified
   cap, the balance identity, `hours ≤ 24`) and must carry a source comment.
4. **Escalation route** ([merge/fuse.py](../../../riskdet/merge/fuse.py)):
   - ABSOLUTE → add the signal id to `integrity_single_family` if it should
     escalate alone.
   - STATISTICAL → relies on ≥2 families or `exceptional_single_family` (all
     gates: leave-cohort-out σ≥6, effect floor, both-halves persistence,
     certified baseline).
   - SUB-FLOOR WATCH → add the family to `NON_CONTRIBUTING_FAMILIES`.
5. **Baseline provenance** — record `baseline_source` (`certified｜empirical｜
   none`) and `threshold_method` on every Signal so the number is reproducible.
6. **Tests** — add to [tests/](../../../tests/): an ABSOLUTE rule needs a test
   that a **sub-floor** instance still fires/escalates (see
   `test_scan_integrity_ungated.py`, `test_fuse_integrity.py`); a STATISTICAL
   rule needs a test that a small-n / low-z instance does **not** escalate.
7. **Report it** — if the rule bounds coverage (floor, sampling, no-retry),
   `log()`/note it so "no findings" is never misread as "clean".

## Guardrails (binding)

- Never invent a fixed numerical threshold for a statistical rule — calibrate it
  to a target alert rate from population quantiles.
- Correlated signals from the same event are ONE family, not independent
  confirmation.
- A rule match is an investigation candidate, never a finding or an enforcement
  action. Final decisions remain 100% human.

## Output when invoked

Given a proposed risk, produce: **class · round-floor(yes/no) · family tag ·
threshold method · escalation route · the files to edit (rule, SQL, thresholds,
fuse) · the tests to add · one benign alternative the skeptic will raise.**
