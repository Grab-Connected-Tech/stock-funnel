# stockfunnel

An evidence-based, guard-railed stock selection funnel for **US (S&P 500)** and **India (Nifty 50/200/500)** equities, built so that even a weak AI model operating it has a hard time making a bad decision.

> Not investment advice. Nothing here guarantees returns. The framework's main job is to stop *avoidable* mistakes (fraud-prone, distressed, illiquid, overpriced and chased stocks, overtrading, oversizing, and narrative-driven overrides) and to tell you honestly when a passive index fund is the better choice.

## The idea in one paragraph

Every decision passes through a fixed funnel. Code makes all quantitative decisions using rules that survived independent academic replication (see [docs/research/](docs/research/)). An AI or human reviewer then examines the survivors, but under **asymmetric authority**: a review can block or shrink a trade, never cause or enlarge one. Malformed reviews count as rejections. Autopilot only trades a paper book with kill switches. Every run, review and trade is written to a hash-chained journal. A built-in backtest with a deflated Sharpe ratio reports whether the strategy actually beats a passive alternative in each market, and that verdict is printed on every report.

## The funnel

| # | Stage | What it does | Evidence |
|---|---|---|---|
| 1 | Universe | Index constituents (S&P 500 / Nifty) | Large/mid caps avoid microcap anomalies that fail replication (Hou-Xue-Zhang 2020) |
| 2 | Regime | Benchmark month-end below its 10-month SMA → max 50% equity. Bear market + volatility spike → momentum weight halved | Faber 2007; Daniel & Moskowitz 2016. VIX/yield-curve timing deliberately excluded as fragile |
| 3 | Tradability | ≥1y history, price floor, liquidity ($10M / ₹10cr daily), market-cap floor, top-decile volatility excluded, no bad price prints in the past year | Lottery stocks underperform; you must be able to exit |
| 4 | Forensic gates | Altman Z″, Beneish M, Piotroski F, Montier C, Sloan accruals, cash generation, dilution, leverage, interest coverage. Separate rules for banks, insurers and utilities. **Fails closed**: unverifiable means rejected | Campbell-Hilscher-Szilagyi 2008: distressed stocks earn *low* returns |
| 5 | Score | Five themes: value, quality, momentum (risk-adjusted 12-1 and 6-1), low vol, shareholder yield. Sector-neutral, winsorized z-scores with frozen per-market weights (India tilts to quality/momentum) | McLean-Pontiff, JKP 2023 themes, NSE factor-index methodologies, IIMA India factor data |
| 6 | Portfolio | Top 30, held until rank > 75 (US) / 60 (IN). Inverse-vol weights, 6–7% name cap, 25% sector cap, ≤2% of daily traded value. 10-day cooling-off before any first buy; no buys after a +25% month | Novy-Marx & Velikov (turnover kills anomalies); Barber & Odean (overtrading) |
| 7 | Review | LLM/human fills a strict schema: checklist (Buffett/Akre/Fundsmith/Marcellus), pre-mortem, kill criteria, evidence citing fact IDs. Veto-only | StockBench, Alpha Arena, FinanceBench: LLMs are poor traders and hallucinate numbers |
| 8 | Execute | Manual: report. Autopilot: paper broker with drawdown kill switches (−15% pauses buys, −25% halts until a human resets), −3% daily-loss pause, stale-data block, idempotent orders, monthly buy and annual turnover budgets, ≤2% of ADV per order, monthly rebalance cadence. Sells are never blocked by budgets | SEC 15c3-5-style pre-trade checks |

The thresholds all live in [policy.py](src/stockfunnel/policy.py). That file is frozen by convention, and no MCP tool can change it.

## Quick start

```bash
uv sync --extra mcp
uv run stockfunnel regime IN                 # market regime
uv run stockfunnel validate nifty200         # 20y walk-forward backtest + deflated Sharpe
uv run stockfunnel run nifty200              # manual mode: recommendations report
uv run stockfunnel analyze TCS.NS            # one stock: verdict, gates, rank vs peers, factor scores
uv run stockfunnel run sp500 --mode autopilot   # paper-trade (BUYs need reviews)
uv run stockfunnel status IN                 # paper book, drawdown, kill-switch state
uv run python tests/test_guardrails.py       # self-check of the guardrails
```

Reports are written to `reports/`, paper books to `state/`, reviews to `reviews/`, and the journal to `journal/`.

### Review loop (how an AI participates)
1. `stockfunnel run <universe>`. BUY candidates come back as *pending review*.
2. `stockfunnel packet <universe> <TICKER>` gives the facts the reviewer may use (no prices, P&L or analyst ratings, to avoid anchoring and herding), the schema, and the instructions.
3. Each reviewer submits JSON, either via MCP `submit_review` or with `stockfunnel review <universe> <TICKER> review.json --reviewer <you>`. **The reviewer ID is stamped by the entry point**, never taken from the JSON: MCP reviews are `mcp:<STOCKFUNNEL_REVIEWER_ID>` and CLI reviews are `cli:<name>`, so one agent can't pose as two reviewers. A review is valid only if:
   - it cites at least 3 decision-relevant facts by ID, including at least one bear point;
   - it has a pre-mortem of 3–6 items;
   - its kill criteria are real metrics.
4. A BUY needs **2 valid PROCEED reviews from distinct reviewers**: for example the MCP agent plus you via the CLI, or two MCP servers configured with different `STOCKFUNNEL_REVIEWER_ID` values backed by different models. The most restrictive review wins.
5. A REJECT, a veto flag or a high-severity pre-mortem creates a **standing veto the moment it's submitted**, whatever the stock's current status. It persists until a human runs `stockfunnel clear-veto`.

**Official vs what-if runs:**
- **Official:** a named universe with no `--limit`, matching the paper book's locked universe. These update the book, the cooling-off watchlist and the vetoes.
- **What-if:** anything else. It reports but never touches state.

**Human-only commands:**
- `reset-halt`, `clear-veto` and `write-off` (for a delisted or renamed holding with no price) require typed confirmation and are not exposed over MCP.
- Any edit or deletion of `state/*.json` outside the code is caught by a hash check against the hash-chained journal. Official runs then refuse to proceed until a human types `ACCEPT`.

**Failure behavior:** missing data never reads as safe or unchanged.
- A held stock with unverifiable fundamentals is held, with no adds.
- A held stock with no recent price makes NAV unknown, which pauses buys. Sells still execute.
- A malformed review blocks the buy without crashing the run.

### Use from Claude / any MCP client
```json
{ "mcpServers": { "stockfunnel": {
    "command": "uv", "args": ["--directory", "/Users/abhilashreddy/Projects/stock-funnel", "run", "stockfunnel-mcp"],
    "env": { "STOCKFUNNEL_REVIEWER_ID": "claude" } } } }
```
Tools: `market_regime`, `analyze_stock`, `run_funnel`, `get_review_packet`, `submit_review`, `portfolio_status`, `explain_policy`. There are deliberately no tools to edit policy, override gates, reset a halt, or place arbitrary orders.

Complementary official MCP servers (see [data-sources.md](docs/research/data-sources.md)):
- Zerodha Kite MCP for Indian broker data
- Alpaca MCP for the US paper/live broker
- EODHD MCP for paid global fundamentals

## Current evidence (run 2026-10-29, `validate`)

This tests the price-based sleeve only: momentum + low vol + lottery exclusion + rank buffer + trend regime. Returns are net of costs over 2008-03 → 2026-10, with bad prints repaired.

| Market | Strategy Sharpe / MaxDD | Equal-weight universe | Index (price only) | Deflated Sharpe | Edge in CSCV half-samples | Verdict |
|---|---|---|---|---|---|---|
| India (Nifty 200) | **1.40 / −20%** | 0.88 / −48% | 0.43 / −52% | 0.998 | 99.9% | **PASSED**: every ±20% parameter variant keeps a +0.43 to +0.50 Sharpe edge |
| US (S&P 500) | 1.00 / **−22%** | 0.95 / −45% | 0.65 / −48% | 0.936 | 46% | **FAILED**: no reliable Sharpe edge; the value is drawdown control |

**How to read this:**
- **India:** the systematic approach shows a robust risk-adjusted edge.
- **US:** it roughly matches equal-weight on Sharpe with half the drawdown. A low-cost equal-weight or multifactor ETF is an equally valid choice, and every US report says so.
- **Trial count:** each re-run of `validate` counts as another trial in the deflated Sharpe, so fiddling until something passes gets harder, not easier. Changing `policy.py` marks old results STALE.

## Known limitations (read before trusting anything)

- **Survivorship bias.** The backtest uses today's constituents, which inflates all absolute numbers. Compare relative results only.
- **Fundamentals are not point-in-time.** yfinance restates figures and keeps only about 4–5 years, so the forensic gates and the value/quality themes are not backtested. Next upgrade: SEC companyfacts (`filed` dates) for the US, and a paid vendor (EODHD) or Screener exports for India.
- **India governance data is missing.** Promoter pledging, ASM/GSM surveillance lists and auditor events aren't in free APIs. Every Indian report lists them as manual checks. Defaulting to Nifty 200 (mostly F&O stocks) reduces this exposure.
- **No estimate-revision factor**, because there is no free history. The framework could snapshot consensus daily to build one.
- **Single writer.** Don't run the CLI and the MCP server's autopilot at the same moment; the last write wins.
- **Tamper anchor is unkeyed.** It stops MCP-only agents and accidents, but an agent with shell access could forge it. Upgrade path: HMAC with a key kept outside the repo.
- **No live broker adapter.** That's deliberate: run the paper book for at least 6 months first. A live adapter (Alpaca / Kite Connect) must implement `broker.execute`'s checks. India: SEBI's retail-algo framework (static IP, OAuth, ≤10 orders/sec, algo-ID registration) applies.

## Layout
```
src/stockfunnel/
  policy.py      frozen thresholds/weights          data.py       yfinance + NSE/S&P lists, disk cache
  accounting.py  Piotroski, Altman Z'', Beneish,    metrics.py    per-ticker metrics
                 Montier C, Sloan accruals          gates.py      hard gates (fail closed)
  regime.py      trend + crash-state                score.py      sector-neutral multi-factor composite
  portfolio.py   buffers, cooling-off, caps         review.py     veto-only LLM/human review
  broker.py      paper broker + kill switches       journal.py    hash-chained decision log
  funnel.py      orchestration                      validate.py   backtest, deflated Sharpe, perturbation
  cli.py / mcp_server.py
docs/research/   factors.md · llm-guardrails.md · data-sources.md (cited briefs)
```
