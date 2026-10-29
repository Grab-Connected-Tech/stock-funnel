# Pre-registration: how the live track record will be judged

Written 2026-10-29, **before** any live results exist. Its SHA-256 fingerprint is recorded in the hash-chained
journal (`kind: preregistration`) and the file is committed to git, so it can't be quietly edited after the fact.
Changing it is allowed only by adding a dated amendment below, never by rewriting the criteria.

## What is being judged
- **Live calls only:** recommendations logged by official runs (`stockfunnel run nifty200` / `sp500`, the scheduled
  daily run) from **2026-10-29** onward. Hindsight replays and backtests do not count.
- **Buy-type calls** (BUY, BUY AFTER, HOLD) and **AVOID** calls, as graded by `stockfunnel scorecard`:
  each call's return minus the market index (`^CRSLDX` Nifty 500 for India, `^GSPC` S&P 500 for the US) over
  3, 6 and 12 months, first call per stock per day only.
- India and the US are judged **separately**.

## Review dates
| Review | Date | Purpose |
|---|---|---|
| 1 | 2027-04-29 (6 months) | Report only. Check the process runs and data is sane. No conclusions. |
| 2 | 2027-10-29 (12 months) | Report only. First 6-month grades. No rule changes except bug fixes. |
| 3 | **2028-10-29 (24 months)** | **Decision.** Apply the criteria below. |

## Success criteria (applied at review 3, per market)
The stock-selection role is **kept** only if ALL hold:
1. At least **30 graded buy-type calls** at the 12-month horizon.
2. Buy-type calls' average 12-month excess return vs the index is **≥ +2 percentage points**
   (a margin to cover trading costs and short-term tax), **and** the 95% confidence interval's lower bound is **> 0**.
3. Buy-type 12-month hit rate (share beating the index) is **≥ 55%**.
4. AVOID calls' average 12-month excess return is **≤ 0** (the gates avoid laggards, not winners).

## What happens otherwise
- If criteria 1–3 fail: the stock-selection role is **retired** for that market. StockFunnel remains a screening
  and risk tool (gates, checks, signals, regime), and the default recommendation is the index or factor ETF.
- If only criterion 4 fails: the gates are reviewed using the gate-attribution replay; any change is decided
  out of sample (e.g. on the other market's data) before adoption.

## Rules until then
- **No changes to scoring weights, gate thresholds or signal promotion** before review 3, except fixes for
  bugs (code that doesn't do what its documentation says). Every change, of any kind, is logged in the journal.
- Tracking-only signals (revision breadth, forward-earnings conviction) may be promoted only at review 3,
  and only if their own `by_signal` grades meet criteria 1–3.
- Replays are for diagnosis. Nothing may be tuned to make a replay look better.

## Amendments
_None yet._
