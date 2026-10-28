"""MCP server so any AI agent (Claude, etc.) can drive the funnel.

Deliberately absent: tools to edit policy, override gates, reset a halt, or place an arbitrary
order. The agent can read, review (veto-only), and trigger paper autopilot runs. That's it.
"""
from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer

from . import broker, data, funnel, journal, policy, regime, review

mcp = MCPServer("stockfunnel", instructions=(
    "Evidence-based stock funnel for US (sp500) and India (nifty50/200/500). Workflow: "
    "1) market_regime 2) run_funnel(mode='manual') 3) for each pending BUY: get_review_packet -> "
    "submit_review (you may only block, never upgrade; a BUY needs 2 valid reviews from distinct reviewer_ids) 4) run_funnel again (manual to recommend, "
    "autopilot to paper-trade). Never quote numbers that aren't in a tool result. Not investment advice."))


def _j(x) -> str:
    return json.dumps(x, indent=2, default=str)


@mcp.tool()
def market_regime(market: str) -> str:
    """Trend + crash-state regime for 'US' or 'IN'. Determines the max equity exposure."""
    return _j(regime.assess(market))


@mcp.tool()
def run_funnel(universe: str, mode: str = "manual") -> str:
    """Run the full funnel. universe: sp500 | nifty500 | nifty200 | nifty50. mode: manual (recommend only)
    or autopilot (executes on the PAPER book, after risk checks). BUYs need 2 valid reviews from distinct reviewers."""
    if universe not in data.UNIVERSES:
        return f"unknown universe; choose one of {sorted(data.UNIVERSES)}"
    try:
        r = funnel.run(universe, mode)
    except (ValueError, RuntimeError) as e:
        return f"refused: {e}"
    from .cli import render
    return render(r)


@mcp.tool()
def get_review_packet(universe: str, ticker: str) -> str:
    """Facts, schema and instructions for reviewing one candidate. Cite fact_ids; don't use outside numbers."""
    if universe not in data.UNIVERSES:
        return f"unknown universe; choose one of {sorted(data.UNIVERSES)}"
    return _j(funnel.review_packet(ticker, funnel.analyze_universe(universe)))


@mcp.tool()
def submit_review(universe: str, ticker: str, review_json: str) -> str:
    """Submit a review (JSON string matching the packet schema). Invalid reviews are stored and BLOCK the buy.
    Multiple reviews per ticker are allowed; the most restrictive wins."""
    if universe not in data.UNIVERSES:
        return f"unknown universe; choose one of {sorted(data.UNIVERSES)}"
    an = funnel.analyze_universe(universe)
    if ticker not in an["metrics"].index:
        return f"{ticker} is not in {universe}"
    try:
        rev = json.loads(review_json)
    except json.JSONDecodeError as e:
        rev, errs = {"unparseable": review_json[:500]}, [f"invalid JSON: {e}"]
    else:
        errs = review.validate(rev, funnel.review_packet(ticker, an))
    funnel.save_review(an["market"], an["as_of"], ticker, rev)
    journal.record("review", {"ticker": ticker, "as_of": an["as_of"], "valid": not errs, "review": rev})
    return _j({"valid": not errs, "errors": errs})


@mcp.tool()
def portfolio_status(market: str) -> str:
    """Paper portfolio for 'US' or 'IN': NAV, drawdown, kill-switch state, weights, watchlist, recent trades."""
    st = broker.load(market)
    last, as_of = data.last_prices(data.prices(list(st["positions"]) or [data.MARKETS[market]["benchmark"]], "1mo")["close"])
    return _j({"risk": broker.risk_status(st, last, as_of), "standing_vetoes": st.get("vetoes", {}), "cash": st["cash"], "weights": broker.weights(st, last),
               "watchlist": st["watchlist"], "recent_trades": st["trades"][-10:]})


@mcp.tool()
def explain_policy() -> str:
    """The frozen rules (gates, weights, sizing, risk limits). Read-only."""
    to_dict = lambda m: {k: (to_dict(v) if hasattr(v, "items") else v) for k, v in m.items()}  # noqa: E731
    return _j({"gates": to_dict(policy.GATES), "weights": to_dict(policy.WEIGHTS),
               "portfolio": to_dict(policy.PORTFOLIO), "risk": to_dict(policy.RISK)})


def main():
    mcp.run()


if __name__ == "__main__":
    main()
