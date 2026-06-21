# Decision-model rebuild — findings & recommendation (Workstream B)

*Generated 2026-06-20. Reproduce: `python scripts/eda_decision_data.py` (B0/B1),
`python scripts/eval_decision_model.py` (B2–B5), `python scripts/train_decision_model.py` (B6, dry-run).
All read-only except the trainer with `--commit`/`--activate`.*

## TL;DR
Ship a **class-weighted multinomial Logistic Regression (L2, C=0.25)** in **shadow
mode**. CV **macro-F1 0.74** (balanced-acc 0.74) vs **rule baseline 0.53** and
**majority 0.20**. KNN scored nominally higher (0.77) but is a **tie within noise**
at n=63, is **badly calibrated**, and **can't serve through the pure-python path**.
LogReg is portable, calibrated, interpretable, and clears the bar. **Do not
auto-apply** — surface as an assistive suggestion only, after validating in shadow.

## Data (n is small — read everything with that caveat)
- **63 usable labels** (latest human_decision per RFP): Proceed 26 / Decline 22 / Park 15.
- **~90% come from the Excel `migration` source** (clean, human-curated). The auto
  scanner contributes only 5 Decline labels; **every Park label is migration**.
  → We train on Excel judgment but serve on noisier scraped `auto` rows
  (**train/serve distribution shift** — the single biggest threat to real-world quality).
- Power: ~12 rows per CV test fold; one misclassification ≈ 0.08 macro-F1. Per-fold
  std ≈ ±0.10. **Differences smaller than ~1 std are noise.**

## B3 — baselines (the bar to clear)
| baseline | macro-F1 |
|---|---|
| majority class (always "Proceed") | 0.20 |
| **rule (`auto_recommendation`) vs human** | **0.53** |

The rule over-Declines (of 26 rule-Declines, 8 were human Park/Proceed) — which is
why we learn raw criteria→human and **exclude `auto_recommendation` as a feature**.

## B4 — model comparison (nested CV, tuned inside folds)
| model | pooled macro-F1 | per-fold mean ± std | Decline / Park / Proceed recall |
|---|---|---|---|
| KNN (k=5, distance) | 0.770 | 0.764 ± 0.103 | 0.77 / 0.73 / 0.81 |
| **LogReg (C=0.25)** | **0.715–0.743** | 0.702 ± 0.101 | 0.86 / 0.60 / 0.77 |
| RandomForest | 0.679 | 0.665 ± 0.086 | 0.82 / 0.53 / 0.69 |
| HistGBM | 0.634 | 0.621 ± 0.095 | 0.86 / 0.40 / 0.65 |

All models separate **Decline** cleanly; **Park is the hard class** (minority +
least-separable — Park vs Proceed criteria means are nearly identical). KNN's only
real edge is Park recall.

## Why LogReg, not the nominally-higher KNN
1. **Tied, not better** — 0.77 vs 0.72/0.74 is well inside one fold's ±0.10 std at n=63.
2. **Calibration** — KNN CV log-loss **2.21, worse than uniform (1.10)**: its
   confidence scores are misleading. An assistive tool that shows confidence must
   be calibrated. LogReg's probabilities are usable.
3. **Portability** — `core.decision_model.predict` is pure-python and serves LINEAR
   models (means/stds/W → softmax). **Verified parity with sklearn to 2e-16.** KNN
   would require shipping the whole training set + sklearn (or a JS KNN) at request
   time, and its later re-implementation for the planned JS frontend is far messier.
4. **Robustness to the train/serve shift** — instance-based methods degrade hardest
   under covariate shift, and we have a real one (migration→auto). LogReg generalizes
   more gracefully.
5. **Interpretability** — per-prediction drivers fall out of the coefficients (shown
   in `predict().drivers`), which the team can sanity-check.

## B2 — text features: not worth it (yet)
TF-IDF(title+description)+LogReg, fit inside folds: **macro-F1 0.49** — worse than
structured. High-dim/small-n. Revisit when labels grow or with the Tier-3 LLM
extraction (the structured-eligibility fields would help far more than raw TF-IDF).

## B5 — dimensionality: keep the full matrix
Full 21 features 0.77 > PCA(95%) 0.75 > SelectKBest(10) 0.67. No reduction helps;
shrinking the feature set *hurts*. Two **zero-variance** columns
(`channel=aggregator-resolved`, `channel=rss`) and a near-dead one (`funder_is_usg`,
because `usg_funders` patterns are unset) carry no signal but are harmless (kept so
the serving feature contract stays stable).

## B6 — serving & deployment
- **Final model:** LogReg, C=0.25 (tuned by CV), class_weight=balanced, L2.
- **Storage:** compact JSON in `app_settings.decision_model` (means/stds/W/classes/
  feature_names/metrics/active). **Serving = pure python** (`core.decision_model.predict`).
- **Shadow first:** `python scripts/train_decision_model.py --commit` stores it with
  `active=False`. The trainer prints whether it beats the rule baseline (it does).
- **Activate only after shadow validation:** `--activate` once it visibly out-performs
  the rule on fresh, mostly-`auto` decisions (not just the migration-heavy backlog).
- **Assistive only** — never auto-applies; the human decision always wins.

## Learning curve & what moves the needle
CV macro-F1 climbs **+0.13** across the sampled training sizes (0.54 → 0.67) — **still
rising, so more labels will help.** The highest-leverage data is **auto-source labels**
(via the verification + search→track flow, Workstream A), which directly attack the
train/serve gap. Fixing the B0 extraction bugs (geo on the auto path, the
`estimated_value` int32 sentinel, the dead `funder_is_usg`) improves serve-time
feature quality.

## Recommended next steps
1. `--commit` to store in **shadow**; watch model-vs-human agreement on new decisions.
2. Grow auto-side labels (Workstream A) and re-train; re-check the time-based split.
3. `--activate` when it beats the rule on auto-heavy data.
4. Defer text features to the LLM-extraction phase.
