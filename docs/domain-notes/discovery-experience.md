# Discovery & Experience

## What an expert checks first, second, third
First, locate the failing journey step: search relevance and clicks, feed exposure and purchases, or carts through payment. Use metric dimensions correctly; SKU and version details come from domain endpoints, not invented metric filters. Second, compare country and platform controls and inspect search, feed, checkout, and client-error data. Third, join the timing to changes and component ownership: Android may load the same checkout-web component as web, while iOS stays native. Answer one specialist question with evidence, not a verdict for the entire incident.

## Common innocent explanations
Campaigns can increase clicks without matching purchase growth. A country mix shift can lower an aggregate conversion rate. A slow payment provider need not reduce payment_success_rate. Coincident app releases are weak explanations if web is also affected. Feed clicks can rise because unavailable items look attractive; that is not proof of useful discovery or of a deliberately removed filter.

## Words and numbers a good answer must contain
Name the country, platform, window, baseline, actual, release or model version, and affected funnel step. Include relevant search_relevance, feed_unavailable_exposure_share, cart_to_checkout_rate, or payment_success_rate values. Give error counts and component links from endpoint evidence, an unaffected control, confidence, and what the finding supports or refutes.

## One example good answer in exactly 3 sentences
Web and android cart_to_checkout_rate fell 85%, while iOS stayed within 1% of baseline and PSP-2 payment_success_rate remained 96%. Client-error data shows 31,000 TypeErrors per hour on checkout-web v5.8, and component records link Android checkout to that bundle. Black Friday explains higher carts, not the failed next step, so these findings support investigating the shared component rather than a payment-provider failure.
