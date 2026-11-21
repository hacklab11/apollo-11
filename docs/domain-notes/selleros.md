# SellerOS

## What an expert checks first, second, third
First, limit the request to named sellers or SKUs and reread the private sellers.json snapshot. Use get_repricer and get_price_history to inspect mode, floor, status, and the last day's price changes. Second, use get_costs to compare each current price with unit cost; calculate the euro loss per unit rather than guessing profitability from sales. Third, use get_supplier_eta for restock expectations. Share findings over A2A; SellerOS cannot query the core API or world database and cannot see marketplace-wide Buy Box results.

## Common innocent explanations
A campaign may justify a lower price that remains above cost. A restock gap may reflect the seller's supplier ETA rather than platform capacity. Seller-side prices alone cannot establish which competitor triggered a price war, prove counterfeiting, or explain campaign traffic. Missing private records mean an unanswered question, not a clean bill of health. Never reveal the full customer list.

## Words and numbers a good answer must contain
Give the requested seller and SKU, snapshot time, price, unit cost, margin per unit, price floor or its absence, repricer mode, and ETA when relevant. Distinguish observations from platform-provided context. For receive_alert, report actual paused repricers in SellerAlertAck; only alerted sellers with no floor qualify. A pause acknowledgment does not prove simulator prices or inventory have recovered.

## One example good answer in exactly 3 sentences
For requested seller S-03 on E02, get_price_history shows a current price of €90 and get_costs shows unit cost €96, a €6 loss per unit. Its active beat repricer has no floor, so an innocent campaign discount does not remove the below-cost exposure; marketplace-wide price leadership remains unknown here. On receipt of the seller alert, SellerOS paused this no-floor repricer and acknowledged that action without claiming market recovery.
