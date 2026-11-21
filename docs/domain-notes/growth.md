# Growth

## What an expert checks first, second, third
First, compare advertiser spend with its cumulative schedule, not an even hourly budget. Inspect budget_pace_ratio and the advertiser's remaining budget. Second, separate ad_spend into ad_clicks and avg_cpc, then clicks into ad_impressions and ad_ctr; inspect auction bidders and slot fill through domain data tools. Third, compare campaign traffic and marketing_cost with platform_discounts and the recorded funding plan. Use metric and changes plus read-only ads and campaign endpoints. Answer the Detective's specific question, not the whole incident.

## Common innocent explanations
A planned morning burst can spend early without a pacing bug. Black Friday can raise traffic, CPC, and coupon costs. Paid social can bring lower-converting sessions even while orders rise. A planned campaign does not automatically explain all lost contribution. Separate platform-funded discounts from seller price cuts, and check whether a spend jump follows the published schedule.

## Words and numbers a good answer must contain
Name advertiser_id where supported, country, category, window, spend in euros, budget used, scheduled share, and CPC in euros per click. State the baseline and plan separately. Findings need confidence, supporting and refuted cause types, and data endpoints used. If auction data or the schedule is unavailable, say so rather than declaring a pacing fault.

## One example good answer in exactly 3 sentences
ADV-3 spent 100% of its daily budget by 10:00 against 38% scheduled, giving budget_pace_ratio 2.63. The recorded schedule rules out an innocent planned morning burst, and changes lists pacing v4.1 before the overspend. This supports a pacing_bug finding for ADV-3; it does not establish a traffic_change cause for the full incident.
