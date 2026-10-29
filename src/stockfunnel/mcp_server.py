"""MCP server so any AI agent (Claude, ChatGPT, Gemini, Cursor ...) can use the funnel.

The judgement protocol (protocol.PROTOCOL) is sent as the server instructions, exposed as a resource,
and baked into the prompts. Every parameter is typed and described (no `from __future__ import
annotations` here: it hides the types from the MCP schema). Deliberately absent: tools to edit policy,
override gates, reset a halt, clear vetoes, or place an arbitrary order.
"""
import json
import os
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from . import broker, data, funnel, policy, regime
from .protocol import FIELD_GUIDE, PROTOCOL, REPLAY_GUIDE, dumps

# Identity of this server's reviews. Run a second server with a different value (or review via the
# CLI yourself) to supply the independent second review a paper-trading BUY requires.
REVIEWER_ID = "mcp:" + os.environ.get("STOCKFUNNEL_REVIEWER_ID", "agent")

mcp = MCPServer("stockfunnel", instructions=PROTOCOL)

Universe = Annotated[Literal["sp500", "nifty50", "nifty200", "nifty500"],
                     Field(description="Index to work in: sp500 = US; nifty50/nifty200/nifty500 = India")]
Market = Annotated[Literal["US", "IN"], Field(description="US or IN (India)")]
Ticker = Annotated[str, Field(description="Yahoo symbol: US plain (AAPL), India with .NS suffix (TCS.NS)")]
Format = Annotated[Literal["json", "markdown"], Field(description="json = structured (see stockfunnel://field-guide); "
                                                                  "markdown = human-readable")]


@mcp.tool()
def market_regime(market: Market) -> str:
    """Trend + crash-state regime. max_equity_exposure < 1 means the system holds cash on purpose."""
    return dumps(regime.assess(market))


@mcp.tool()
def analyze_stock(ticker: Ticker,
                  universe: Annotated[Literal["sp500", "nifty50", "nifty200", "nifty500"] | None,
                                      Field(description="peer universe; default sp500 for US, nifty200 for India")] = None,
                  format: Format = "json") -> str:
    """The full, final recommendation for ONE stock: action, conviction, size, why, risks, sell triggers, automated
    checks, hidden signals, gates, rank vs peers, factor scores, regime and evidence. The action is final: never
    upgrade it."""
    r = funnel.analyze_stock(ticker.strip().upper(), universe)
    if format == "markdown":
        from .cli import render_stock
        return render_stock(r)
    return dumps(r)


@mcp.tool()
def run_funnel(universe: Universe,
               mode: Annotated[Literal["manual", "autopilot"],
                               Field(description="manual = recommend only; autopilot = trade the PAPER book")] = "manual",
               format: Format = "json") -> str:
    """Screen a whole index: every top-30 basket and held stock's recommendation, plus the top-30 basket (alphabetical)."""
    try:
        r = funnel.run(universe, mode)
    except (ValueError, RuntimeError) as e:
        return f"refused: {e}"
    if format == "markdown":
        from .cli import render
        return render(r)
    return dumps(r)


@mcp.tool()
def plan_investment(amount: Annotated[float, Field(description="money to invest, in the market's currency (INR or USD)", gt=0)],
                    market: Market = "IN",
                    months: Annotated[int, Field(description="intended holding period in months", ge=1)] = 3,
                    target_monthly_return: Annotated[float | None, Field(
                        description="hoped-for monthly return as a decimal, e.g. 0.01 for 1%/month; answered with "
                                    "historical odds, never a forecast")] = None,
                    fractional_shares: Annotated[bool | None, Field(
                        description="allow fractional shares; default yes for US, no for India")] = None,
                    risk: Annotated[Literal["conservative", "moderate", "growth"] | None, Field(
                        description="risk appetite: caps the share put into stocks so a repeat of the index's worst "
                                    "period of this length would cost at most 10% (conservative) / 20% (moderate) of the "
                                    "whole amount; growth or omitted = no cap. Changes how much, never which stocks")] = None) -> str:
    """Turn an amount of money into a concrete plan: which stocks and how many shares (or the index fund when the amount
    is too small to diversify), cash held back by the market regime, buy-from dates, and HISTORICAL odds of reaching a
    hoped-for return. Use this for any 'I have X to invest' question. Never present the odds as a forecast."""
    from .plan import plan_investment as build
    return dumps(build(float(amount), market, int(months), target_monthly_return, fractional=fractional_shares, risk=risk))


@mcp.tool()
def get_review_packet(universe: Universe, ticker: Ticker) -> str:
    """Facts, schema and instructions for reviewing one paper-trading candidate. Cite fact_ids only."""
    return dumps(funnel.review_packet(ticker, funnel.analyze_universe(universe)))


@mcp.tool()
def submit_review(universe: Universe, ticker: Ticker,
                  review_json: Annotated[str, Field(description="JSON string matching the review packet's schema")]) -> str:
    """Submit a review. It can only BLOCK a paper-trading buy. Your reviewer_id is set by the server; REJECTs and veto
    flags persist until a human clears them."""
    try:
        rev = json.loads(review_json)
    except json.JSONDecodeError as e:
        rev = f"invalid JSON: {e}"
    return dumps(funnel.record_review(universe, ticker, rev, reviewer_id=REVIEWER_ID))


@mcp.tool()
def portfolio_status(market: Market) -> str:
    """Paper portfolio: NAV, drawdown, kill-switch state, weights, watchlist, recent trades."""
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
def replay_benchmark(universe: Universe = "sp500",
                     months_back: Annotated[list[int], Field(
                         description="look-back windows in months, e.g. [3, 6, 12]; 36+ is often data-limited",
                         min_length=1, max_length=6)] = [3, 6, 12],  # noqa: B006 - schema default, never mutated
                     compare_tickers: Annotated[list[str] | None, Field(
                         description="optional tickers to benchmark over the same windows, e.g. your own picks "
                                     "['AAPL', 'MSFT'] or India ['TCS.NS']")] = None) -> str:
    """HINDSIGHT TEST: for each look-back, rebuild what the framework would have held using only data available on that
    date, and return its return to today next to the equal-weight universe, real benchmark ETFs (with dividends) and
    your compare_tickers, all over the same period.

    Returns JSON: windows[] with as_of, data_sufficient, returns{name: decimal}, framework_minus{name: framework minus
    that benchmark, positive = framework did better}, framework_top5[], framework_holdings[] (weight, return),
    your_tickers{}; plus method and caveats. Full field meanings: resource stockfunnel://replay-guide.
    Never present as part of the live track record. Slow: ~1 minute per window."""
    from .replay import benchmark
    return dumps(benchmark(universe, months_back, compare_tickers))


@mcp.tool()
def replay_monthly(universe: Universe = "sp500",
                   months: Annotated[int, Field(description="how many month-end start dates to replay", ge=6, le=36)] = 24) -> str:
    """HINDSIGHT TEST over many start dates: replay at every month-end for the last `months`, grading each over the next
    3 months. Returns summary{top5_vs_ew, top30_vs_ew, avoided_vs_ew: {avg, median, share_right, windows}} and by_date[].
    *_vs_ew = return minus the equal-weight universe (decimal); share_right = share of windows where picks beat it
    (for avoided: where they lagged it). Steadier than single windows, still hindsight. Slow: several minutes."""
    from .replay import multi_date
    return dumps(multi_date(universe, months))


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


@mcp.resource("stockfunnel://replay-guide", name="replay-guide", mime_type="application/json",
              description="Meaning of every field returned by replay_benchmark and replay_monthly")
def replay_guide_resource() -> str:
    return dumps(REPLAY_GUIDE)


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


@mcp.prompt(name="benchmark_framework",
            description="Hindsight-test the framework against the index, ETFs and optionally your own picks")
def benchmark_framework(universe: str = "sp500", compare_tickers: str = "") -> str:
    tickers = [t.strip() for t in compare_tickers.split(",") if t.strip()]
    return (f"{PROTOCOL}\nReplay guide:\n{dumps(REPLAY_GUIDE)}\n\nTask: call replay_benchmark(universe=\"{universe}\", "
            f"months_back=[3, 6, 12]{', compare_tickers=' + json.dumps(tickers) if tickers else ''}). For each window give, "
            "as a small table: the framework_portfolio return, each benchmark's return, and framework_minus (say plainly "
            "whether the framework beat or lagged each). Mark any data_sufficient=false window as unavailable. Then, if "
            "useful, call replay_monthly for a steadier read. Close by stating this is a hindsight test, not the live "
            "track record, repeating the caveats briefly, and 'not financial advice'.")


def main():
    mcp.run()


if __name__ == "__main__":
    main()
