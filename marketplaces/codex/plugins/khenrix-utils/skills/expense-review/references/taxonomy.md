# Expense category taxonomy (compact Swedish household)

The 14 seed categories (in `category`, applied by `002_seed.sql`). Resolution order when reviewing a
transaction: **merchant default (cached) → MCC (if the feed carried it) → descriptor rules below →
LLM-in-loop proposal → user confirms**. A confirmed category is written back onto the `merchant` so the
next transaction from that vendor auto-fills. Always strip payment-processor prefixes
(`KLARNA *`, `PAYPAL *`, `IZ */ZETTLE *`, `SUMUP *`, `STRIPE *`) before matching — see `normalize.py`.

| slug | name | typical Swedish merchants | MCC hints |
|---|---|---|---|
| `dagligvaror` | Dagligvaror | ICA, Coop, Willys, Hemköp, Lidl, City Gross, Mathem | 5411, 5499 |
| `restaurang` | Restaurang & Café | Espresso House, Waynes, restaurants, Foodora, Uber Eats, McDonald's, MAX | 5812, 5814, 5811 |
| `transport` | Transport & Resor | SL, SJ, Västtrafik, Skånetrafiken, Uber, Bolt, Taxi, parking (EasyPark, Aimo), Tåg | 4111, 4121, 4131, 7523 |
| `drivmedel` | Drivmedel | Circle K, OKQ8, Preem, St1, Ingo, Tesla Supercharger, charging | 5541, 5542, 5552 |
| `boende` | Boende & Hem | rent/avgift, IKEA, Bauhaus, Clas Ohlson, Jula, Hornbach, Rusta | 5200, 5211, 5712 |
| `el-internet` | Hushållsel & Internet | Vattenfall, Ellevio, E.ON, Telia, Tele2, Telenor, Bahnhof, Comhem | 4900, 4814 |
| `halsa` | Hälsa & Apotek | Apoteket, Apotek Hjärtat, Kronans Apotek, Lloyds, gym (SATS, Nordic Wellness), vårdcentral | 5912, 8062, 7997, 8011 |
| `shopping` | Shopping & Kläder | H&M, Zalando, Lindex, Stadium, XXL, Åhléns, Amazon, Elgiganten, NetOnNet | 5651, 5691, 5732, 5999 |
| `noje` | Nöje & Fritid | bio (SF Bio), Steam, PlayStation, books (Akademibokhandeln), events (Ticketmaster) | 5815, 7832, 7922, 5942 |
| `systembolaget` | Systembolaget | Systembolaget | 5921 |
| `prenumerationer` | Prenumerationer | Netflix, Spotify, HBO Max, Disney+, Viaplay, iCloud, Google One, YouTube Premium, ChatGPT | 4899, 5968, 7372 |
| `avgifter` | Avgifter & Ränta | bank fees, årsavgift, ränta, övertrasseringsavgift, valutapåslag, FX fees | 6012, 6051 |
| `resa` | Resa | flights (SAS, Norwegian), hotels (Booking, Airbnb, Scandic), car rental, SJ long-distance | 3000-3299, 3501-3999, 4511, 7011 |
| `ovrigt` | Övrigt | anything unclear; internal transfers/refunds → mark `ignored`/`is_transfer`, not a category | — |

## Notes
- **Multi-line-of-business merchants** can't trust a single default: ICA also has ICA Banken (→ `avgifter`),
  Circle K sells food (→ `restaurang` not `drivmedel`). Use MCC/amount/context, and let the user confirm.
- **Swish**: a payment *method*, not a category — categorize by counterparty/purpose, or `ignored` if it's a
  transfer/reimbursement between own people (keep it off the shared balance via `is_transfer`).
- **Internal transfers** (e.g. Swedbank → SAS card payment) and **reimbursements** are `ignored`
  (`is_transfer=true`) — never a spend category. **Refunds/credits need care:** a refund of a *shared*
  purchase must be **split symmetrically** (positive `shareable`, opposite sign) so it credits Anna back —
  blanket-ignoring it leaves her still owing her half. Only refunds of *personal/ignored* spend are ignored.
- MCC is *opportunistic*: present on card-rail feeds (often Amex), frequently absent on bank feeds.
