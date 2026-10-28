# LLM-agent guardrails & autopilot safety (researched 2026-10-29)

## Evidence: LLMs as traders are weak and biased
- StockBench (arXiv 2510.02209): GPT-5, Claude-4, Qwen3, etc. mostly fail to beat buy-and-hold.
- LiveTradeBench (21 LLMs): reasoning-benchmark scores don't translate to trading.
- Alpha Arena (nof1) S1: 4/6 frontier models lost real money; leverage decided outcomes. S1.5 (US stocks): one model profitable.
- InvestorBench (ACL 2025): all backbones struggle in volatile regimes.
- Lopez-Lira & Tang: LLM headline sentiment predicts returns, but alpha decays with adoption; ~37% of the apparent effect is memorization (arXiv 2512.23847).
- FinanceBench: GPT-4-Turbo + retrieval failed/refused 81% of questions → never trust model-computed numbers.
- Documented biases: memorization/look-ahead, hallucinated figures, sycophancy, herding toward analyst ratings, anchoring, recency, disposition effect (*stronger* in more capable models), run-to-run inconsistency.

## Mitigations implemented in this framework
| Pattern | Where |
|---|---|
| Deterministic core; LLM is narrator/vetoer only | `funnel.py`, `portfolio.py` |
| Asymmetric authority: final = min(quant, every LLM sample) | `review.py` |
| Schema-or-nothing: invalid review ⇒ NO_ACTION | `review.py` |
| Fact grounding: evidence must cite code-supplied fact IDs | `review.py` |
| Ensemble: most restrictive sample wins | `review.py` |
| Cooling-off: ≥10 days on watchlist before first buy; no buy after +25% month | `portfolio.py` |
| Turnover budget, position/sector caps, no leverage/shorts | `policy.py` |
| Drawdown kill switch: −15% pauses buys, −25% halts autopilot (human reset) | `broker.py` |
| Order sanity: limit orders within ±2% of last price, ≤1% ADV, idempotent per decision | `broker.py` |
| Hash-chained decision journal | `journal.py` |
| Prompt hygiene: no purchase price, P&L, analyst ratings in review context | `funnel.review_packet` |

## Regulation notes
- **US**: SEC Rule 15c3-5 is the template for pre-trade checks; SEC IM Guidance 2017-02 (robo-advisers). Advising third parties likely makes you an investment adviser.
- **India**: SEBI circular Feb 4 2025 (retail algo trading), fully applicable ~Apr 1 2026: broker-registered algos above 10 orders/sec/exchange, static IP, OAuth + 2FA, daily re-login, exchange algo ID; black-box algos offered to others need SEBI RA registration. Keep ≤5 orders/sec. Verify latest NSE circulars before going live.

## Process lessons from top investors (encoded as checklist / gates)
- Fundsmith: good companies, don't overpay, do nothing (high ROCE, FCF conversion, low turnover).
- Akre three-legged stool: great business, honest capable management, reinvestment runway.
- Berkshire: circle of competence, moat, margin of safety, inversion.
- Marcellus CCP (India): forensic screen first, ROCE > cost of capital every year, few names, trade rarely.
- Concentrated/leveraged "conviction" styles (e.g. Jhunjhunwala) are not reproducible → structurally barred.

Sources: arXiv 2510.02209, 2511.03628, 2412.18174, 2304.07619, 2512.23847, 2504.14765; SEBI circular SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013; FINRA 2025 oversight report; SEC IM 2017-02; Marcellus CCP deck.
