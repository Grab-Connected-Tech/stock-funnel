"""The judgement protocol for any LLM using this tool, plus a field guide to its outputs.
Single source of truth: served as MCP instructions, an MCP resource and prompt, and over HTTP."""
from __future__ import annotations

import json

import numpy as np


def dumps(obj) -> str:
    """JSON for LLM clients: NaN -> null, numpy scalars -> plain numbers, anything else -> string."""
    def clean(o):
        if isinstance(o, float) and o != o:
            return None
        if isinstance(o, dict):
            return {str(k): clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, np.generic):
            return clean(o.item())
        return o
    return json.dumps(clean(obj), indent=2, default=str)

PROTOCOL = """\
stockfunnel: evidence-based stock recommendations for US (S&P 500) and India (Nifty 50/200/500).
All numbers and decisions are computed by deterministic code from official data. You interpret; you don't decide.

HOW TO JUDGE A STOCK
1. Call analyze_stock(ticker, format="json"). India tickers end in .NS (TCS.NS); US are plain (AAPL).
2. Lead with recommendation.action_label (the action, marked "(no proven edge)" where applicable). It is final. Never upgrade it: AVOID/SELL/WAIT never become BUY,
   BUY AFTER never becomes "buy now". You may be MORE cautious, and must say why.
3. Explain using recommendation.why, .risks, .sell_if, .signals and .checks, in plain language.
   Quote only numbers that appear in the tool output. Do not compute, estimate or recall prices,
   ratios or targets yourself.
4. You MAY add real-world context the tool cannot see (recent news, industry developments,
   management changes). Label it clearly as "Outside the tool:" and never let it change the action
   upward. If that context is a serious red flag, say so and advise caution.
5. Always state: recommendation.conviction and its conviction_points; position_size if any;
   recommendation.alternative if present (it means a cheap index/factor ETF is an equally good choice);
   recommendation.evidence_scope (what is and isn't backtested), and that this is not personal financial advice.
6. If action is BUY AFTER, the start_date is a deliberate cooling-off period. Do not shorten it.

"I HAVE <AMOUNT> TO INVEST" QUESTIONS
- Call plan_investment(amount, market, months, target_monthly_return, risk). Pass months = when they need the money
  and risk (conservative / moderate / growth) when they state an appetite; if they don't, ask or leave it out.
  Lead with recommended_route and
  route_reason. If it says "index fund", say so first; small amounts can't diversify across 30 stocks.
- Never promise or predict a return. Answer return hopes with historical_odds (how often the index got
  there over that horizon, and how often it lost money), described as history, not a forecast.
- Repeat every item in notes (horizon, tax, risk-off cash). Present positions with shares, cost and buy_from.

HOW TO SCREEN A MARKET
- market_regime(market) first: if max_equity_exposure < 1, the system keeps cash on purpose.
- run_funnel(universe, format="json"): actions per stock, plus top_ranked (the top-30 basket, alphabetical).
  Present BUY / BUY AFTER names with their reasons as a BASKET: never imply a best pick or present an order
  (hindsight replays show the order inside the basket carries no information). Never add names.

HINDSIGHT REPLAYS (replay_benchmark, replay_monthly)
- These rewind the framework to past dates and grade it against what happened since. Use them to benchmark
  the framework against the index, factor ETFs, or your own picks (compare_tickers) over the same period.
- Always call them "hindsight tests". Never merge them with track_record (the real, recorded-in-advance calls).
- Respect data_sufficient=false windows (say the framework result isn't available) and repeat the caveats.
- framework_minus: positive = the framework did better than that benchmark. Field meanings: stockfunnel://replay-guide.
- One window is an anecdote; prefer replay_monthly or several windows before drawing conclusions.

HONESTY RULES
- evidence.status (from analyze_stock) / validation (from run_funnel) says whether the strategy has a
  proven edge in that market. If FAILED or STALE, say so plainly.
- Gates and checks marked UNKNOWN mean data was unavailable, not that the stock is safe.
- track_record() shows how past calls actually performed; cite it when asked about reliability.
- Never claim certainty about future prices.

YOU CANNOT (by design): change policy, override gates, clear vetoes, reset kill switches, or place
trades outside the paper book. submit_review can only block a paper-trading BUY, never create one.
"""

FIELD_GUIDE = {
    "recommendation.action_label": "the action as the headline; buys carry '(no proven edge)' where the backtest failed",
    "recommendation.action": "BUY | BUY AFTER <start_date> (cooling-off) | WAIT (good but not now) | HOLD (keep if owned) "
                             "| SELL (exit if owned) | AVOID (don't buy)",
    "recommendation.conviction": "HIGH | MEDIUM | LOW: a transparent points tally (conviction_points), not a probability",
    "recommendation.position_size": "suggested fraction of the whole portfolio (0.018 = 1.8%), already halved in risk-off markets",
    "recommendation.sell_if": "pre-committed exit triggers; when one trips the system sells, no debate",
    "recommendation.sell_date": "for India: a rank-based sale deferred to qualify for long-term capital-gains tax",
    "recommendation.alternative": "present when no backtested edge exists: an index/factor ETF is an equally valid choice",
    "recommendation.checks": "automated official-source checks (NSE surveillance, price band, pledges, auditor events; "
                             "SEC restatements, auditor changes, going concern). PASS | WARN | FAIL | UNKNOWN",
    "recommendation.signals": "hidden signals: insider buying, corporate red-flag filings, price tells, analyst revisions, "
                              "upcoming results date. POSITIVE | NEUTRAL | WARN | WAIT | FAIL | UNKNOWN. "
                              "revision_breadth is TRACKING ONLY: shown and graded, but it does not affect the action",
    "verdict": "funnel STAGE, not the action: TOP RANKED (top 30) | HOLD ZONE | BELOW BUFFER | REJECT (failed a gate). The action is recommendation.action",
    "blocked": "true when the stock ranked well on its statements but a later pre-trade check vetoed it (action AVOID); "
               "lead with the block, not the stage",
    "rank / ranked_peers": "internal position among gate-passing peers, used only for tier rules (top 30 = buy zone, "
                           "hold zone up to 60 IN / 75 US). Don't present the number: only the tier carries information",
    "themes": "factor z-scores vs sector peers (0 = average, +1 = better than ~84%, -1 = worse than ~84%): "
              "value, quality, momentum, low_vol, yield",
    "gates.checks": "hard safety gates. Any FAIL rejects; more than one UNKNOWN rejects (fail-closed)",
    "regime.max_equity_exposure": "share of money the system allows in stocks now (0.5 = half in cash)",
    "evidence.status": "PASSED only if BOTH the long price-factor backtest and the recent full-funnel replay pass; "
                       "MIXED if they disagree; FAILED if neither; STALE/NOT_RUN otherwise. Anything but PASSED means "
                       "no proven edge (buys are labelled so)",
    "metrics": "raw inputs (ROE, debt/equity, P/E, Altman Z'', Beneish M, momentum, volatility ...)",
}


REPLAY_GUIDE = {
    "what_it_is": "Hindsight test: the framework is rebuilt as of a past date using only data available then, and "
                  "graded on what happened since. Kept separate from track_record (real calls recorded in advance).",
    "units": "all returns are decimals over the window: 0.05 = +5%, -0.12 = -12%",
    "replay_benchmark.windows[].as_of": "the past date the framework was rebuilt on (start of the window)",
    "replay_benchmark.windows[].graded_to": "latest data date (end of the window)",
    "replay_benchmark.windows[].data_sufficient": "false = too few companies had published statements by as_of to run "
                                                  "the full funnel; the framework result is omitted. Report it as unavailable",
    "replay_benchmark.windows[].regime_then": "market regime on as_of; risk_off means the framework held part cash",
    "replay_benchmark.windows[].equity_exposure_then": "share of money the framework had in stocks (rest in cash at 0%)",
    "replay_benchmark.windows[].returns.framework_portfolio": "the framework's actual portfolio: top 30 at inverse-volatility "
                                                             "weights, regime-scaled, held unchanged to graded_to",
    "replay_benchmark.windows[].returns.framework_top5_equal_weight": "its 5 best-ranked names, equal weight (noisy)",
    "replay_benchmark.windows[].returns.equal_weight_universe": "average stock in today's index members (survivorship-flattered)",
    "replay_benchmark.windows[].returns.<ETF name>": "real benchmark ETFs including dividends (US: SPY, RSP; India: "
                                                     "NIFTYBEES, JUNIORBEES)",
    "replay_benchmark.windows[].returns.your_tickers_equal_weight": "equal-weight average of compare_tickers",
    "replay_benchmark.windows[].framework_minus": "framework_portfolio minus each other return: POSITIVE = the framework "
                                                  "did better, NEGATIVE = it lagged. Absent when data_sufficient is false",
    "replay_benchmark.windows[].framework_top5": "the 5 best-ranked names then, each with its own return",
    "replay_benchmark.windows[].framework_holdings": "every holding then: ticker, weight, return",
    "replay_benchmark.windows[].your_tickers": "each compare_ticker's return (null if no price data)",
    "replay_monthly.summary.<group>_vs_ew": "group's 3-month return minus the equal-weight universe, per start date; "
                                            "avg / median across dates",
    "replay_monthly.summary.<group>.share_right": "share of start dates where top5/top30 beat equal-weight, or where "
                                                  "avoided stocks lagged it (higher = better for every group)",
    "replay_monthly.ranking": "does the ORDER of the ranking carry information? Measured only among gate-passing "
                              "stocks: avg_rank_correlation (>0 = higher-ranked did better), share of windows positive, "
                              "top30_vs_rest_of_passed, average return by quintile, per-factor correlations, and "
                              "informative (true only if all point the right way)",
    "replay_monthly.by_date": "one row per start date: as_of, regime, ranked count, and the three *_vs_ew values",
    "how_to_read": "One window is an anecdote. Prefer several windows or replay_monthly before concluding anything, and "
                   "always repeat the caveats.",
}
