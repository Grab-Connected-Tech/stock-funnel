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
| 4 | Forensic gates | Altman Z″, Beneish M, Piotroski F, Montier C, Sloan accruals, cash generation, dilution, leverage, interest coverage. Separate rules for banks, insurers, utilities and REITs (REITs use FFO and net debt/EBITDA, because depreciation distorts their book figures). **Fails closed**: an unverifiable forensic or tradability gate means rejected; at most one unknown is tolerated among the other solvency items | Campbell-Hilscher-Szilagyi 2008: distressed stocks earn *low* returns |
| 5 | Score | Five themes: value, quality, momentum (risk-adjusted 12-1 and 6-1), low vol, shareholder yield. Sector-neutral, winsorized z-scores with frozen per-market weights (India tilts to quality/momentum) | McLean-Pontiff, JKP 2023 themes, NSE factor-index methodologies, IIMA India factor data |
| 6 | Portfolio | Top 30, held until rank > 75 (US) / 60 (IN). Inverse-vol weights, 6–7% name cap, 25% sector cap, ≤2% of daily traded value. 10-day cooling-off before any first buy; no buys after a +25% month | Novy-Marx & Velikov (turnover kills anomalies); Barber & Odean (overtrading) |
| 7 | Review | LLM/human fills a strict schema: checklist (Buffett/Akre/Fundsmith/Marcellus), pre-mortem, kill criteria, evidence citing fact IDs. Veto-only | StockBench, Alpha Arena, FinanceBench: LLMs are poor traders and hallucinate numbers |
| 8b | Automated checks | India: NSE surveillance lists (ASM/GSM/ESM), price band / circuit lock-in, promoter pledges, auditor resignations and forensic audits. US: SEC restatements (8-K 4.02), auditor changes (4.01), going-concern language. Free official sources, no keys | Replaces the manual "before real money" checklist |
| 8c | Hidden signals | Footprints events leave before headlines: insider open-market buying clusters (NSE PIT / Yahoo insider data), the exchange's own red-flag filing categories (fraud, default, insolvency, delayed results, results queries; US late-filing notices, bankruptcy, delisting, impairment), price tells (falling while the sector isn't, volume shocks), and 90-day analyst estimate revisions. Severe events veto, shocks mean WAIT, soft flags cost conviction, positives add at most one point | Cohen-Malloy-Pomorski 2012, Lakonishok-Lee 2001, Alford-Jones-Zmijewski 1994, Chan-Jegadeesh-Lakonishok 1996. Insider selling, news sentiment and social trends deliberately excluded |
| 9 | Recommendation | One action (BUY · BUY AFTER date · WAIT · HOLD · SELL · AVOID), conviction as a transparent points tally, position size, why/risks, explicit sell triggers, and the ETF alternative where no edge is proven. Pure rules, no LLM | Every input above |
| 10 | Track record | Every official call is journaled. `stockfunnel scorecard` grades them against the market at 1/3/6/12 months | The only legitimate way to tune later: live, out-of-sample results |
| 8 | Execute | Manual: report. Autopilot: paper broker with drawdown kill switches (−15% pauses buys, −25% halts until a human resets), −3% daily-loss pause, stale-data block, idempotent orders, monthly buy and annual turnover budgets, ≤2% of ADV per order, monthly rebalance cadence. Sells are never blocked by budgets | SEC 15c3-5-style pre-trade checks |

The thresholds all live in [policy.py](src/stockfunnel/policy.py). That file is frozen by convention, and no MCP tool can change it.

## Quick start

```bash
uv sync --extra mcp
uv run stockfunnel regime IN                 # market regime
uv run stockfunnel validate nifty200         # 20y walk-forward backtest + deflated Sharpe
uv run stockfunnel run nifty200              # manual mode: recommendations report
uv run stockfunnel analyze TCS.NS            # one stock: verdict, gates, tier vs peers, factor scores
uv run stockfunnel run sp500 --mode autopilot   # paper-trade (BUYs need reviews)
uv run stockfunnel status IN                 # paper book, drawdown, kill-switch state
uv run python tests/test_guardrails.py       # offline self-check of the guardrails
uv run python tests/regression_live.py       # live regression checks (network): tools agree, sector rules, data honesty
```

Reports are written to `reports/`, paper books to `state/`, reviews to `reviews/`, and the journal to `journal/`.

### Web UI
```bash
uv run stockfunnel-ui        # then open http://127.0.0.1:8765
```
Four tabs:
- **Analyze:** any stock's verdict, tier meter, factor scores, a plain-English summary, safety gates, and a manual-check list saved per stock.
- **Screener:** runs the funnel on an index.
- **Portfolio:** the paper book and the cooling-off watchlist.
- **Evidence:** the backtest verdicts.

The UI only reads and recommends. It never trades, edits policy or clears vetoes. It binds to localhost only. Set `STOCKFUNNEL_PORT` to change the port.

### Review loop (how an AI participates)
1. `stockfunnel run <universe>`. BUY candidates come back as *pending review*.
2. `stockfunnel packet <universe> <TICKER>` gives the facts the reviewer may use (no prices, P&L or analyst ratings, to avoid anchoring and herding), the schema, and the instructions.
3. Each reviewer submits JSON, either via MCP `submit_review` or with `stockfunnel review <universe> <TICKER> review.json --reviewer <you>`. **The reviewer ID is stamped by the entry point**, never taken from the JSON: MCP reviews are `mcp:<STOCKFUNNEL_REVIEWER_ID>` and CLI reviews are `cli:<name>`. An agent with only MCP access therefore can't pose as two reviewers. The *operator* can, though, by configuring two MCP servers, so independence is only as real as the setup. For genuine independence, make the second review yours via the CLI, or back the two servers with different models. A review is valid only if:
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

### Use from Claude Desktop / any MCP client
Claude Desktop is configured in `~/Library/Application Support/Claude/claude_desktop_config.json`. Use the full path to `uv`, because desktop apps don't inherit your shell PATH. Restart Claude Desktop after editing. Example prompt: *"I have ₹10,000 to invest in Indian stocks and hope for 1% a month over the next 3 months. Use stockfunnel."*
```json
{ "mcpServers": { "stockfunnel": {
    "command": "uv", "args": ["--directory", "/path/to/stock-funnel", "run", "stockfunnel-mcp"],
    "env": { "STOCKFUNNEL_REVIEWER_ID": "claude" } } } }
```
**For any LLM (Claude, ChatGPT, Gemini, Cursor, ...):**
- **Tools:** `market_regime`, `analyze_stock`, `run_funnel`, `get_review_packet`, `submit_review`, `portfolio_status`, `track_record`, `explain_policy`. `analyze_stock` and `run_funnel` return structured JSON by default; pass `format: "markdown"` for prose.
- **Judgement protocol:** sent automatically as the server's instructions, and also available as the resource `stockfunnel://protocol`. In short, the tool decides and the model explains. The model must never upgrade an action, may quote only numbers from the tool, and must label any outside knowledge as "Outside the tool:", which can only add caution. It is defined in [protocol.py](src/stockfunnel/protocol.py).
- **Field guides:** `stockfunnel://field-guide` explains every field in the analysis and screen output, and `stockfunnel://replay-guide` explains every replay field. All tool parameters are typed, with allowed values and descriptions, so agents can't guess wrong.
- **Hindsight replays:**
  - `replay_benchmark(universe, months_back, compare_tickers)` rebuilds what the framework would have held at each look-back, using only that date's data. It returns the framework's performance to today next to the equal-weight universe, real benchmark ETFs and any tickers you pass, with `framework_minus` gaps for easy benchmarking.
  - `replay_monthly` repeats the replay at every month-end.
  - These are hindsight tests, kept out of the live track record. CLI equivalent: `stockfunnel replay sp500 --benchmark 3,6,12 --compare AAPL,MSFT`.
- **`plan_investment(amount, market, months, target_monthly_return)`** turns an amount into share counts with buy-from dates. It recommends the index ETF when the amount is too small to diversify, holds back cash in risk-off markets, and answers return hopes with historical odds, never a forecast.
- **Prompts:** `judge_stock(ticker)`, `screen_market(universe)` and `benchmark_framework(universe, compare_tickers)` walk a model through the protocol step by step.
- **Without MCP:** the web server publishes an OpenAPI spec at `http://127.0.0.1:8765/openapi.json`, with the protocol in its description. The endpoints are `/api/analyze`, `/api/regime`, `/api/run`, `/api/validation`, `/api/status`, `/api/track_record` and `/api/protocol`.

There are deliberately no tools to edit policy, override gates, reset a halt, clear vetoes, or place arbitrary orders.

Complementary official MCP servers (see [data-sources.md](docs/research/data-sources.md)):
- Zerodha Kite MCP for Indian broker data
- Alpaca MCP for the US paper/live broker
- EODHD MCP for paid global fundamentals

## Current evidence (run 2026-10-29, `validate`)

This tests the price-based sleeve only: momentum + low vol + lottery exclusion + rank buffer + trend regime. Returns are net of costs over 2008-03 → 2026-10, with bad prints repaired.

| Market | Strategy Sharpe / MaxDD | Equal-weight universe | Best investable ETF (≥5y overlap) | Verdict |
|---|---|---|---|---|
| India (Nifty 200) | **1.41 / −20%** | 0.88 / −48% | Nifty100 Low Vol 30: 0.89 vs strategy 1.28 (2018+) | **PASSED** all 6 checks, including worst-case tax: 14.4% vs the ETF's 12.2% a year |
| US (S&P 500) | 1.00 / −22% | 0.95 / −45% | USMV 1.05, QUAL 0.95, MTUM 0.94 vs strategy 0.93–1.03 | **FAILED**: a factor ETF does as well with no effort |

**How to read this:**
- **India:** a robust risk-adjusted edge, though survivorship bias inflates it (only today's index members were tested). After worst-case short-term tax, the margin over the low-volatility ETF is only about 2 points a year, small enough that survivorship bias could plausibly account for it.
- **US:** use USMV or QUAL as the default. The stock picks are an alternative, not an improvement.
- **Trial count:** each re-run of `validate` counts as another trial in the deflated Sharpe. Changing `policy.py` marks old results STALE.

### Full-funnel hindsight replay (24 month-ends, each graded over the next 3 months)

The backtest above covers price factors only. `stockfunnel replay <universe> --multi 24` rebuilds the **whole** funnel
(gates, fundamentals, ranking) at each past month-end from statements that were public by then. The US replay uses
the S&P 500 as it was on each date (no survivorship bias); India uses today's members.

| | US (S&P 500) | India (Nifty 200) |
|---|---|---|
| Top-30 basket vs equal-weight | −0.5% avg, beat it in 55% of windows | −1.7% avg, beat it in 18% |
| Top-5 vs equal-weight | −2.0%, 27% | −2.1%, 32% |
| Stocks the gates avoided vs equal-weight | **+0.9%**, lagged in only 18% | +0.8%, lagged in 45% |
| Ranking informative? (rank correlation, share of windows positive) | Yes, weakly (+0.085, 82%) | No (−0.037, 36%) |

**What this says:**
- **The order inside the top 30 carries no information** (top 5 does worse than top 30), so the top 30 is shown as an
  alphabetical basket, and outputs use tiers (top-30 basket / hold zone / below) instead of rank numbers.
- **US:** the ranking has a small real signal, but the gates gave it back. Excluded stocks beat the market even with
  historical index membership, so it isn't survivorship. Per-gate attribution: the volatility ("lottery") exclusion
  removed stocks that beat the market by 6.5% on average in this rally; the leverage gates did avoid laggards.
- **India:** the forensic gates (Beneish, Montier, Altman, Piotroski) did avoid laggards, but the ranking ran backwards.
- **Evidence status** combines both tests: PASSED only if both pass. Both markets are currently **MIXED, no proven
  edge**, and every buy says so. Two years of replays are one market regime; nothing was tuned to them.

## Pre-registered success criteria

[docs/PREREGISTRATION.md](docs/PREREGISTRATION.md) fixes, before any live results exist, how the live track record
will be judged (decision review 2028-10-29: ≥30 graded calls, ≥ +2 pp average 12-month excess with a positive CI lower
bound, ≥55% hit rate, AVOIDs ≤ 0) and freezes scoring and gates until then except bug fixes. Its fingerprint is in
the hash-chained journal.

## Tuning principles (what changed in the tuning pass, and what deliberately didn't)

No weight or threshold was re-fitted to the backtest. That would raise backtest numbers and worsen live results, which is what the deflated Sharpe guards against. The tuning pass instead fixed departures from the research:
- **Evidence scoped honestly.** The backtest covers price factors only, so conviction credits it only when a stock's strengths include momentum or low volatility.
- **Fresher fundamentals.** Valuation and profitability use trailing 12 months when 4 consecutive quarters exist. The forensic gates stay on audited annual statements.
- **Right benchmark.** Validation now compares against investable factor ETFs, not just equal-weight.
- **One verdict.** `analyze_stock`, `run_funnel` and `plan_investment` all compute a stock's recommendation in one place (`funnel.stock_report`), so they can't disagree. The paper book's own decision is shown separately as `paper_action`.
- **Results timing.** A buy waits when results came out after the data snapshot, are due within days, or a quarter is unreported inside the legal deadline with no date published.
- **Data honesty.** Missing analyst-revision counts read "unavailable" (with source coverage), never "0 up, 0 down".
- **Conservative earnings.** For non-REITs, when consensus expects profit to fall, value and quality are judged on the lower forward figure. There's no threshold, and it only works one way: forecasts can lower a valuation, never raise it. REITs are excluded because their GAAP EPS swings with depreciation; they're judged on FFO.
- **Normalized earnings.** When current profit is more than 1.5× its multi-year median (cyclical peaks such as refiners, or one-off gains), value and quality are judged on the average, and the stock is flagged "possible peak-cycle earnings" (Graham).
- **Tax-aware selling (India).** A rank-based sell of a winner within 60 days of its 1-year mark waits for the 12.5% long-term rate instead of 20%. Safety-gate sells never wait.
- **Track record.** Recommendations are logged and graded out of sample, including each hidden signal on its own (`scorecard` → `by_signal`). A signal that doesn't prove itself within a year should be removed.

## Known limitations (read before trusting anything)

- **Beneish false alarms.** Beneish's receivables index (DSRI) blows up when receivables grow off a tiny base (e.g. Oberoi Realty 2026: ₹113 cr → ₹324 cr, about 5% of sales). The gate stays strict by design: missing one good stock is cheap, owning a manipulator is not, and loosening a published model after seeing which stock it rejects is curve-fitting. If you want to own a stock rejected this way, check the annual report yourself (receivable ageing, related-party sales, cash conversion). That decision sits outside the system, on purpose.
- **Survivorship bias.** The backtest uses today's constituents, which inflates all absolute numbers. Compare relative results only.
- **Fundamentals are not point-in-time.** yfinance restates figures and keeps only about 4–5 years, so the forensic gates and the value/quality themes are not backtested. Next upgrade: SEC companyfacts (`filed` dates) for the US, and a paid vendor (EODHD) or Screener exports for India.
- **Not covered by the automated checks:** qualified audit opinions and related-party transactions (India), which live only in PDF annual reports; SEC enforcement actions (US), which aren't indexed by ticker. If a critical check's source is unreachable, the recommendation becomes WAIT (fail-closed). For US stocks outside the S&P 500, set `STOCKFUNNEL_SEC_UA="your-name your-email"` (SEC policy).
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
