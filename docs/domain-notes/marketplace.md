# Marketplace

## What an expert checks first, second, third
First, inspect offers, prices, Buy Box holders, and demand-weighted availability for the affected category and seller tiers. Separate a tier losing share from the entire market losing sales. Second, compare purchase-order quantity with forecast, deliveries with orders, supplier delays, and FBA blocked units. Third, order price changes and stockouts in time, inspect newly joined sellers, and compare unaffected categories or tiers. Use read-only offers, inventory, purchasing, metric, and changes tools; ask for SellerOS evidence through the Detective when private costs or floors matter.

## Common innocent explanations
Promotion demand can empty stock without a supplier failure. A normal delivery arriving against an undersized purchase order is not evidence of a delay. A price cut can be planned, while competitors' subsequent losses are not. A new low-price offer is not proof of counterfeiting, and rising 3P Buy Box share may simply follow a 1P stockout.

## Words and numbers a good answer must contain
Give category, seller_tier, seller and SKU ids when supported by offer data, time, prices, quantities, and availability. Name po_qty_vs_forecast, po_fill_rate, supplier_delay_hours, or fba_blocked_units when relevant. Quantify buybox_share movements in percentage points. Findings should state confidence, evidence against alternatives, and the remaining gap rather than claim the whole incident's root cause.

## One example good answer in exactly 3 sentences
Home po_qty_vs_forecast fell to 0.30 after purchasing v2.3, while delivered purchase orders show po_fill_rate 1.00. Home 1P in_stock_rate fell from 95% to 40%, but home 3P availability stayed at 94%. Stable home searches and on-time deliveries weaken demand and supplier-delay explanations; the SUP-4 delay concerns toys SKU T05, not these home stockouts.
