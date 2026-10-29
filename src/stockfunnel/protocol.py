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
- Call plan_investment(amount, market, months, target_monthly_return). Lead with recommended_route and
  route_reason. If it says "index fund", say so first; small amounts can't diversify across 30 stocks.
- Never promise or predict a return. Answer return hopes with historical_odds (how often the index got
  there over that horizon, and how often it lost money), described as history, not a forecast.
- Repeat every item in notes (horizon, tax, risk-off cash). Present positions with shares, cost and buy_from.

HOW TO SCREEN A MARKET
- market_regime(market) first: if max_equity_exposure < 1, the system keeps cash on purpose.
- run_funnel(universe, format="json"): actions per stock, plus top_ranked. Present BUY / BUY AFTER
  names with their reasons; never add names the funnel didn't propose.

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
    "verdict": "funnel stage result: BUY CANDIDATE (top 30) | HOLD ZONE | PASS (not a buy) | REJECT (failed a gate)",
    "rank / ranked_peers": "position among peers that passed every safety gate (1 = best)",
    "themes": "factor z-scores vs sector peers (0 = average, +1 = better than ~84%, -1 = worse than ~84%): "
              "value, quality, momentum, low_vol, yield",
    "gates.checks": "hard safety gates. Any FAIL rejects; more than one UNKNOWN rejects (fail-closed)",
    "regime.max_equity_exposure": "share of money the system allows in stocks now (0.5 = half in cash)",
    "evidence.status": "PASSED | FAILED | STALE | NOT_RUN: backtest of the price factors vs equal-weight and factor ETFs",
    "metrics": "raw inputs (ROE, debt/equity, P/E, Altman Z'', Beneish M, momentum, volatility ...)",
}
