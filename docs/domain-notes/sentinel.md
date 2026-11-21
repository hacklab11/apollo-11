# Sentinel

## What an expert checks first, second, third
First, read the anomaly and divergent-segment scans together: healthy totals can hide a falling small_3P share. Check freshness before interpreting any drop. Second, compare actual and expected values, z scores, persistence, and estimated euro impact to choose the most important candidate. Third, inspect events, graph links, and open incidents; attach downstream symptoms rather than opening duplicate investigations. Use baseline_compare for an extra check. Sentinel triages; it does not prove a cause.

## Common innocent explanations
Hourly seasonality, a planned Black Friday campaign, and delayed partitions can all move a chart. A campaign is context, not permission to ignore an anomaly: the Skeptic must quantify its effect. Defer incomplete data; watch a borderline or single-hour spike and recheck next tick. A healthy total does not clear a damaged segment.

## Words and numbers a good answer must contain
Name the metric, supported segment, observation hour, actual, expected, delta, z score, completeness, and impact in euros per hour. State investigate, watch, defer, ignore, or attach, with a reason and incident id when attaching. Distinguish relative percent changes from percentage-point changes in shares.

## One example good answer in exactly 3 sentences
FR orders are 400 against 1,000 expected this hour, but check_freshness reports only 40% completeness. The late partition is a data-delay explanation, so these counts cannot establish a business loss. Defer this candidate and recheck after backfill rather than opening a sales incident.
