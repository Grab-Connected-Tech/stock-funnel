"""MCP server so any AI agent (Claude, ChatGPT, Gemini, Cursor ...) can use the funnel.

The judgement protocol (protocol.PROTOCOL) is sent as the server instructions, exposed as a resource,
and baked into the judge_stock / screen_market prompts. Deliberately absent: tools to edit policy,
override gates, reset a halt, clear vetoes, or place an arbitrary order.
"""
from __future__ import annotations

import json
import os

from mcp.server.mcpserver import MCPServer

from . import broker, data, funnel, policy, regime
from .protocol import FIELD_GUIDE, PROTOCOL, dumps

# Identity of this server's reviews. Run a second server with a different value (or review via the
# CLI yourself) to supply the independent second review a paper-trading BUY requires.
REVIEWER_ID = "mcp:" + os.environ.get("STOCKFUNNEL_REVIEWER_ID", "agent")

mcp = MCPServer("stockfunnel", instructions=PROTOCOL)


def _bad_universe(universe: str) -> str | None:
    return None if universe in data.UNIVERSES else f"unknown universe; choose one of {sorted(data.UNIVERSES)}"


@mcp.tool()
def market_regime(market: str) -> str:
    """Trend + crash-state regime for 'US' or 'IN'. max_equity_exposure < 1 means the system holds cash on purpose."""
    return dumps(regime.assess(market))


@mcp.tool()
def analyze_stock(ticker: str, universe: str | None = None, format: str = "json") -> str:
    """The full, final recommendation for ONE stock (e.g. 'AAPL', 'TCS.NS'): action, conviction, size, why, risks,
    sell triggers, automated checks, hidden signals, gates, rank vs peers, factor scores, regime and evidence.
    format: 'json' (structured; see resource stockfunnel://field-guide) or 'markdown' (human-readable).
    The action is final: never upgrade it."""
    if universe is not None and (err := _bad_universe(universe)):
        return err
    r = funnel.analyze_stock(ticker.strip().upper(), universe)
    if format == "markdown":
        from .cli import render_stock
        return render_stock(r)
    return dumps(r)


@mcp.tool()
def run_funnel(universe: str, mode: str = "manual", format: str = "json") -> str:
    """Screen a whole index. universe: sp500 | nifty500 | nifty200 | nifty50. mode: manual (recommend only) or
    autopilot (executes on the PAPER book after risk checks). format: 'json' or 'markdown'."""
    if err := _bad_universe(universe):
        return err
    try:
        r = funnel.run(universe, mode)
    except (ValueError, RuntimeError) as e:
        return f"refused: {e}"
    if format == "markdown":
        from .cli import render
        return render(r)
    return dumps(r)


@mcp.tool()
def plan_investment(amount: float, market: str = "IN", months: int = 3, target_monthly_return: float | None = None) -> str:
    """Turn an amount of money into a concrete plan: which stocks and how many shares (or the index fund when the amount
    is too small to diversify), cash held back by the market regime, buy-from dates, and HISTORICAL odds of reaching a
    hoped-for return (e.g. target_monthly_return=0.01 for 1%/month). Use this for any 'I have X to invest' question.
    market: 'IN' or 'US'. Never present the odds as a forecast."""
    if market not in ("IN", "US"):
        return "market must be 'IN' or 'US'"
    from .plan import plan_investment as build
    return dumps(build(float(amount), market, int(months), target_monthly_return))


@mcp.tool()
def get_review_packet(universe: str, ticker: str) -> str:
    """Facts, schema and instructions for reviewing one paper-trading candidate. Cite fact_ids only."""
    if err := _bad_universe(universe):
        return err
    return dumps(funnel.review_packet(ticker, funnel.analyze_universe(universe)))


@mcp.tool()
def submit_review(universe: str, ticker: str, review_json: str) -> str:
    """Submit a review (JSON string matching the packet schema). It can only BLOCK a paper-trading buy.
    Your reviewer_id is set by the server; REJECTs and veto flags persist until a human clears them."""
    try:
        rev = json.loads(review_json)
    except json.JSONDecodeError as e:
        rev = f"invalid JSON: {e}"
    return dumps(funnel.record_review(universe, ticker, rev, reviewer_id=REVIEWER_ID))


@mcp.tool()
def portfolio_status(market: str) -> str:
    """Paper portfolio for 'US' or 'IN': NAV, drawdown, kill-switch state, weights, watchlist, recent trades."""
    st = broker.load(market)
    last, as_of = data.last_prices(data.prices(list(st["positions"]) or [data.MARKETS[market]["benchmark"]], "1mo")["close"])
    return dumps({"risk": broker.risk_status(st, last, as_of), "standing_vetoes": st.get("vetoes", {}), "cash": st["cash"],
                  "weights": broker.weights(st, last), "watchlist": st["watchlist"], "recent_trades": st["trades"][-10:]})


@mcp.tool()
def track_record() -> str:
    """How past recommendations performed vs the market (live, out-of-sample), by action and by hidden signal."""
    from . import scorecard
    return dumps(scorecard.grade())


@mcp.tool()
def explain_policy() -> str:
    """The frozen rules (gates, weights, sizing, risk limits). Read-only."""
    to_dict = lambda m: {k: (to_dict(v) if hasattr(v, "items") else v) for k, v in m.items()}  # noqa: E731
    return dumps({"gates": to_dict(policy.GATES), "weights": to_dict(policy.WEIGHTS),
                  "portfolio": to_dict(policy.PORTFOLIO), "risk": to_dict(policy.RISK)})


@mcp.resource("stockfunnel://protocol", name="judgement-protocol", mime_type="text/plain",
              description="How an LLM must use and present this tool's output")
def protocol_resource() -> str:
    return PROTOCOL


@mcp.resource("stockfunnel://field-guide", name="field-guide", mime_type="application/json",
              description="Meaning of every field in analyze_stock / run_funnel JSON")
def field_guide_resource() -> str:
    return dumps(FIELD_GUIDE)


@mcp.prompt(name="judge_stock", description="Judge one stock the right way: the tool decides, you explain")
def judge_stock(ticker: str) -> str:
    return (f"{PROTOCOL}\nField guide:\n{dumps(FIELD_GUIDE)}\n\nTask: judge {ticker}. Call analyze_stock(\"{ticker}\", "
            "format=\"json\"), then answer in this order: 1) the action exactly as given (with start/sell date if any), "
            "conviction and position size; 2) why, in plain language; 3) risks and the sell triggers; 4) checks and "
            "signals worth noting; 5) evidence status and any alternative; 6) optional 'Outside the tool:' context, "
            "clearly labelled, which may only add caution; 7) a one-line 'not financial advice' note.")


@mcp.prompt(name="screen_market", description="Screen an index and present the funnel's picks faithfully")
def screen_market(universe: str = "nifty200") -> str:
    return (f"{PROTOCOL}\n\nTask: call market_regime for the market of {universe}, then run_funnel(\"{universe}\", "
            "format=\"json\"). Report the regime, the evidence status, and every BUY / BUY AFTER with its reason and "
            "target weight. Do not add, remove or re-rank names. End with the data-coverage note and 'not financial advice'.")


def main():
    mcp.run()


if __name__ == "__main__":
    main()
