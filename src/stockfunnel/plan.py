"""Turn an amount of money into a concrete, honest plan: shares to buy, what's unaffordable, cash held
back by the regime, and, instead of a return promise, how often the market historically delivered the
hoped-for return over that horizon. Pure data, no forecasts."""
from __future__ import annotations

import math
from . import broker, data, funnel
from .policy import PORTFOLIO
from .portfolio import capped_weights

MIN_PLACED = 0.8  # the stock route needs at least this share of the equity budget actually placed in stocks
INDEX_NAME = {"^CRSLDX": "Nifty 500", "^GSPC": "S&P 500"}
INDEX_FUND = {"IN": ("NIFTYBEES.NS", "Nifty 50 index ETF"), "US": ("RSP", "S&P 500 equal-weight ETF")}
BUYABLE = ("BUY", "BUY AFTER")
TEMPORARY = ("WAIT",)  # results due, a volume shock...: still part of the plan, bought once it clears
# Risk appetite sets HOW MUCH goes into stocks, never WHICH stocks: the most of the whole amount the investor
# accepts losing if the worst historical period of their holding length repeated. growth = no cap (the default).
RISK_MAX_LOSS = {"conservative": 0.10, "moderate": 0.20, "growth": None}


def stock_share(market: str, months: int, risk: str | None) -> tuple[float, dict | None]:
    """Share of the money allowed in stocks so that a repeat of the index's worst `months`-month period would
    cost at most RISK_MAX_LOSS[risk] of the whole amount (the rest sits in cash / a liquid fund at ~0%).
    ponytail: ~20 years of one index's history; the real stock basket can fall more than the index."""
    if risk is None:
        return 1.0, None
    cap, bench = RISK_MAX_LOSS[risk], data.MARKETS[market]["benchmark"]
    worst = horizon_odds(bench, INDEX_NAME[bench], months, None)["worst"]
    share = 1.0 if cap is None or worst >= 0 else min(1.0, cap / -worst)
    return share, {"profile": risk, "max_loss_accepted": cap, "horizon_months": months,
                   "index_worst_return_over_horizon": worst, "stock_share_before_regime": round(share, 3)}


def horizon_odds(ticker: str, name: str, months: int, target_monthly: float | None) -> dict:
    """Distribution of `ticker`'s rolling `months`-month returns over up to ~20 years of its own history."""
    px = data.prices([ticker], "20y")["close"][ticker].dropna()
    monthly = px.resample("ME").last()
    r = (monthly.shift(-months) / monthly - 1).dropna()
    out = {"index": name, "ticker": ticker, "horizon_months": months, "windows": len(r),
           "history_from": str(monthly.index[0].date()),
           "median_return": round(float(r.median()), 4), "worst_10pct": round(float(r.quantile(0.10)), 4),
           "worst": round(float(r.min()), 4), "best": round(float(r.max()), 4),
           "chance_of_loss": round(float((r < 0).mean()), 3)}
    if target_monthly is not None:
        goal = (1 + target_monthly) ** months - 1
        out["target_total_return"] = round(goal, 4)
        out["chance_of_reaching_target"] = round(float((r >= goal).mean()), 3)
    return out


def plan_investment(amount: float, market: str = "IN", months: int = 3, target_monthly_return: float | None = None,
                    universe: str | None = None, fractional: bool | None = None, risk: str | None = None) -> dict:
    """fractional: allow fractional shares (default: yes for US brokers, no for India).
    risk: conservative | moderate | growth caps the stock share by historical worst loss; None = unchanged default."""
    fractional = market == "US" if fractional is None else fractional
    universe = universe or funnel.DEFAULT_PEERS[market]
    a = funnel.analyze_universe(universe)
    reg, sc, last = a["regime"], a["scored"], a["last_prices"]
    n = PORTFOLIO["n_holdings"][market]
    book = sc[sc["rank"] <= n]
    w = capped_weights(1 / book["vol_1y"], book["sector"], PORTFOLIO["max_weight"][market], PORTFOLIO["max_sector_weight"])
    share, profile = stock_share(market, months, risk)
    w = w[w >= PORTFOLIO["min_weight"]] * reg["max_equity_exposure"] * share
    st = broker.load(market)
    equity_budget = amount * reg["max_equity_exposure"] * share

    positions, waiting, unaffordable, blocked = [], [], [], []
    for t, wt in w.sort_values(ascending=False).items():
        price, budget = last.get(t), wt * amount
        if not price:
            continue
        rec = funnel.stock_report(t, a, st, log=False)["recommendation"]  # same verdict as analyze_stock / run
        shares = round(budget / price, 4) if fractional else math.floor(budget / price)
        row = {"ticker": t, "name": a["metrics"].at[t, "name"], "weight": round(float(wt), 4),
               "budget": round(budget, 2), "price": round(price, 2), "shares": shares, "cost": round(shares * price, 2),
               "action": rec["action"], "buy_from": rec["start_date"] or a["as_of"]}
        reason = rec["why"][0] if rec["why"] else rec["action"]
        if shares <= 0:
            unaffordable.append(row)
        elif rec["action"] in TEMPORARY:
            waiting.append(row | {"buy_from": "when the wait clears", "reason": reason})
        elif rec["action"] not in BUYABLE:
            blocked.append(row | {"reason": reason})
        else:
            positions.append(row)

    placed = sum(p["cost"] for p in positions + waiting)
    placed_frac = placed / equity_budget if equity_budget else 0.0
    validation = funnel.latest_validation(market)
    proven = validation.get("status") == "PASSED"
    stock_route = proven and placed_frac >= MIN_PLACED

    etf, etf_name = INDEX_FUND[market]
    etf_px = data.prices([etf], "1mo")["close"][etf].dropna()
    etf_price = float(etf_px.iloc[-1]) if len(etf_px) else None
    units = lambda money: (round(money / etf_price, 4) if fractional else math.floor(money / etf_price)) if etf_price else None  # noqa: E731
    # stock route: the ETF tops up only what stocks can't take (unaffordable / avoided); money for names on a
    # temporary WAIT stays in cash until they clear.
    etf_money = (equity_budget - placed) if stock_route else equity_budget
    odds = (horizon_odds(data.MARKETS[market]["benchmark"], INDEX_NAME[data.MARKETS[market]["benchmark"]], months,
                         target_monthly_return) if stock_route else
            horizon_odds(etf, etf_name, months, target_monthly_return))

    notes = []
    if proven and not stock_route:
        notes.append(f"Only {placed_frac:.0%} of the {equity_budget:,.0f} equity budget could go into the funnel's stocks "
                     f"({len(positions) + len(waiting)} of {len(w)} names: {len(unaffordable)} unaffordable, {len(blocked)} "
                     "avoided). Below 80% the portfolio loses the diversification the strategy depends on, so the index "
                     "fund is the better route.")
    if stock_route and etf_money > 0:
        notes.append(f"{etf_money:,.0f} of the equity budget couldn't be placed in stocks ({len(unaffordable)} unaffordable, "
                     f"{len(blocked)} avoided) and goes into {etf_name} instead of sitting in cash.")
    if stock_route and waiting:
        notes.append(f"{len(waiting)} names are on a temporary WAIT (e.g. results due); their "
                     f"{sum(p['cost'] for p in waiting):,.0f} stays in cash until each wait clears, then gets re-checked.")
    if months < 12:
        notes.append(f"The strategy is built and tested for holding periods of a year or more; {months} months is "
                     "mostly market noise." + (" In India, selling within 12 months is also taxed at 20% (vs 12.5%)."
                                               if market == "IN" else ""))
    if reg["max_equity_exposure"] < 1:
        notes.append(f"The market is risk-off: the system keeps {1 - reg['max_equity_exposure']:.0%} of the money in cash "
                     "or a liquid fund until the trend recovers.")
    if profile:
        p = profile
        notes.append(f"Risk profile '{risk}': " + (
            "no cap on the stock share." if p["max_loss_accepted"] is None else
            f"the worst {months}-month period in the index's history returned {p['index_worst_return_over_horizon']:.0%}; "
            f"with {share:.0%} in stocks a repeat would cost about {share * -min(p['index_worst_return_over_horizon'], 0):.0%} "
            f"of the whole amount (limit {p['max_loss_accepted']:.0%}). The profile changes how much goes into stocks, "
            "not which stocks."))
    if target_monthly_return is not None:
        notes.append(f"No system can promise {target_monthly_return:.1%} a month. Historically, {odds['index']} "
                     f"reached {odds['target_total_return']:.1%} over {months} months in "
                     f"{odds['chance_of_reaching_target']:.0%} of periods, and lost money in {odds['chance_of_loss']:.0%}.")

    return {
        "amount": amount, "market": market, "universe": universe, "as_of": a["as_of"], "regime": reg["label"],
        "fractional_shares": fractional,
        "recommended_route": "stock portfolio" if stock_route else "index fund",
        "route_reason": (f"{placed_frac:.0%} of the equity budget is placed across {len(positions)} stocks and the strategy "
                         "has a validated edge here" if stock_route else
                         "no validated edge in this market: the index fund is the evidence-based default" if not proven else
                         f"only {placed_frac:.0%} of the equity budget can be placed in stocks (need 80%)"),
        "index_fund": {"ticker": etf, "name": etf_name, "unit_price": round(etf_price, 2) if etf_price else None,
                       "amount": round(etf_money, 2), "units": units(etf_money)},
        "stock_portfolio": {"positions": positions if stock_route else [], "waiting": waiting if stock_route else [],
                            "considered": len(w),
                            "placed": round(placed, 2) if stock_route else 0.0, "placed_fraction": round(placed_frac, 3),
                            "unaffordable": unaffordable if stock_route else [], "avoided": blocked if stock_route else []},
        "cash_or_liquid_fund": round(amount - equity_budget, 2),
        **({"risk_profile": profile} if profile else {}),
        "historical_odds": odds,
        "evidence": validation.get("status"),
        "notes": notes,
        "disclaimer": "Historical frequencies, not forecasts. Not personal financial advice.",
    }
