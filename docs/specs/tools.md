# Deterministic numerical tools

Implementation: `packages/data/src/eye/tools/`; typed operations:
`packages/data/src/eye/warehouse/operations.py`. Inputs/results are validated;
numerical evidence comes from these tools, not model arithmetic.

## Seasonal baselines and scans (`stats.py`)

- Prefer the same hour in the previous four weeks, requiring at least three complete references.
  Otherwise use the previous seven days' same hour, with weekday factors from 28 days of history.
- Center is based on median numerator/denominator; spread is `1.4826 × MAD`.
  `z = (actual - expected) / max(spread, 0.02 × abs(expected), 1e-9)`.
  Baseline bands are center ± three spreads.
- References require completeness at least 0.9. Defaults: point-scan z threshold 3,
  divergent-segment threshold 3 percentage points, minimum completeness 0.9, top ten signals.
- Anomaly priority is `abs(z) × max(impact_weight, 0.05) × completeness`;
  impact scaling is metric-aware, not a universal conversion of every delta to EUR.
- Ratios pool components. Missing/zero-denominator values are not fabricated observations.

## Attribution and segmentation (`drill.py`)

Default comparison windows are one week apart unless explicit baseline windows are supplied.
Sum identities attribute each signed component delta. Positive product identities attribute
`exponent × log(component_after/component_before) / log(target_after/target_before)`;
target log change below `1e-6` uses equal shares. A residual remains explicit.
Nonpositive product inputs fail validation.

For a ratio grouped by a dimension, with denominator share `w` and segment rate `r`:
`mix = Σ(w1-w0)r0`, `rate = Σw1(r1-r0)`. Positive denominator totals are required;
a nonzero numerator with zero denominator is rejected.
Drill-down ranks absolute contribution and marks positive-contribution segments until
at least 80% of total delta is covered. First-change and comparison tools report measured
windows and controls; they do not assert causality. First-change requires two consecutive
hours with absolute z at least 3 and returns the first of those hours.

## Validation and context (`validate.py`, `context.py`)

Identity validation excludes supplied incident hours and observations below 90% completeness.
It requires nonempty residuals, median relative residual below 0.005 and p95 below 0.02,
including populated groups. Rollup validation requires maximum residual below 0.005.
Lagged correlation removes a same-hour seven-day median seasonal factor, then first-differences
the normalized series. It checks lags 0–96 hours by default, requires at least three pairs
with nonzero variance, and returns the largest absolute Pearson correlation. This is evidence
for a causal candidate, not proof of causation.
Freshness checks distinguish late/incomplete partitions from business losses.
Planned-effect checks require the expected direction and effect within ±25% for a full match;
opposite direction explains nothing. See [Skeptic](core-agents.md).

The scenario truth table is forbidden as a data source, including through tool SQL.
