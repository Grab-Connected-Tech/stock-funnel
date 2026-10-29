"""MCP server so any AI agent (Claude, etc.) can drive the funnel.

Deliberately absent: tools to edit policy, override gates, reset a halt, or place an arbitrary
order. The agent can read, review (veto-only), and trigger paper autopilot runs. That's it.
"""
from __future__ import annotations

import json
import os

from mcp.server.mcpserver import MCPServer

from . import broker, data, funnel, policy, regime

# Identity of this server's reviews. Run a second server with a different value (or review via the
# CLI yourself) to supply the independent second review a BUY requires.
REVIEWER_ID = "mcp:" + os.environ.get("STOCKFUNNEL_REVIEWER_ID", "agent")

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
def analyze_stock(ticker: str, universe: str | None = None) -> str:
    """Full verdict for ONE stock (e.g. 'AAPL', 'TCS.NS'): hard gates, rank and factor scores vs its peer
    universe (default sp500 / nifty200), regime, and status in the paper book. Read-only."""
    if universe is not None and universe not in data.UNIVERSES:
        return f"unknown universe; choose one of {sorted(data.UNIVERSES)}"
    from .cli import render_stock
    return render_stock(funnel.analyze_stock(ticker, universe))


@mcp.tool()
def get_review_packet(universe: str, ticker: str) -> str:
    """Facts, schema and instructions for reviewing one candidate. Cite fact_ids; don't use outside numbers."""
    if universe not in data.UNIVERSES:
        return f"unknown universe; choose one of {sorted(data.UNIVERSES)}"
    return _j(funnel.review_packet(ticker, funnel.analyze_universe(universe)))


@mcp.tool()
def submit_review(universe: str, ticker: str, review_json: str) -> str:
    """Submit a review (JSON string matching the packet schema). Invalid reviews are stored and BLOCK the buy.
    Your reviewer_id is set by the server, not by you: a BUY needs a second, independent reviewer
    (a human via the CLI, or another separately configured MCP server). REJECTs and veto flags persist."""
    try:
        rev = json.loads(review_json)
    except json.JSONDecodeError as e:
        rev = f"invalid JSON: {e}"
    return _j(funnel.record_review(universe, ticker, rev, reviewer_id=REVIEWER_ID))


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
