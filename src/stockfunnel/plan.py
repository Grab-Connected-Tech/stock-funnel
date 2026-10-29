"""Turn an amount of money into a concrete, honest plan: shares to buy, what's unaffordable, cash held
back by the regime, and, instead of a return promise, how often the market historically delivered the
hoped-for return over that horizon. Pure data, no forecasts."""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np

from . import broker, checks, data, funnel, signals
from .policy import PORTFOLIO
from .portfolio import capped_weights

MIN_AFFORDABLE_SHARE = 0.6  # below this share of the book being buyable, the amount is too small to diversify
INDEX_NAME = {"^CRSLDX": "Nifty 500", "^GSPC": "S&P 500"}
INDEX_FUND = {"IN": ("NIFTYBEES.NS", "Nifty 50 index ETF"), "US": ("RSP", "S&P 500 equal-weight ETF")}


def horizon_odds(market: str, months: int, target_monthly: float | None) -> dict:
    """Distribution of the benchmark's rolling `months`-month returns over ~20 years (price only)."""
    bench = data.MARKETS[market]["benchmark"]
    px = data.prices([bench], "20y")["close"][bench].dropna()
    monthly = px.resample("ME").last()
    r = (monthly.shift(-months) / monthly - 1).dropna()
    out = {"index": INDEX_NAME.get(bench, bench), "horizon_months": months, "windows": len(r),
           "median_return": round(float(r.median()), 4), "worst_10pct": round(float(r.quantile(0.10)), 4),
           "worst": round(float(r.min()), 4), "best": round(float(r.max()), 4),
           "chance_of_loss": round(float((r < 0).mean()), 3)}
    if target_monthly is not None:
        goal = (1 + target_monthly) ** months - 1
        out["target_total_return"] = round(goal, 4)
        out["chance_of_reaching_target"] = round(float((r >= goal).mean()), 3)
    return out


def plan_investment(amount: float, market: str = "IN", months: int = 3, target_monthly_return: float | None = None,
                    universe: str | None = None) -> dict:
    universe = universe or funnel.DEFAULT_PEERS[market]
    a = funnel.analyze_universe(universe)
    reg, sc, last = a["regime"], a["scored"], a["last_prices"]
    n = PORTFOLIO["n_holdings"][market]
    book = sc[sc["rank"] <= n]
    w = capped_weights(1 / book["vol_1y"], book["sector"], PORTFOLIO["max_weight"][market], PORTFOLIO["max_sector_weight"])
    w = w[w >= PORTFOLIO["min_weight"]] * reg["max_equity_exposure"]
    watch = broker.load(market)["watchlist"]
    as_of = date.fromisoformat(a["as_of"])

    positions, unaffordable = [], []
    for t, wt in w.sort_values(ascending=False).items():
        price, budget = last.get(t), wt * amount
        if not price:
            continue
        shares = math.floor(budget / price)
        start = date.fromisoformat(watch[t]) if t in watch else as_of
        buy_from = max(start + timedelta(days=PORTFOLIO["cooling_off_days"]), as_of)
        row = {"ticker": t, "name": a["metrics"].at[t, "name"], "rank": int(sc.at[t, "rank"]), "weight": round(float(wt), 4),
               "budget": round(budget, 2), "price": round(price, 2), "shares": shares, "cost": round(shares * price, 2),
               "buy_from": buy_from.isoformat()}
        (positions if shares >= 1 else unaffordable).append(row)

    # Gate-level checks for the names that would actually be bought (cached daily).
    for p in positions:
        res = {**checks.run(p["ticker"]), **signals.run(p["ticker"], a["metrics"])}
        blockers = [f"{k.replace('_', ' ')}: {v['detail']}" for k, v in res.items()
                    if v["status"] in ("FAIL", "WAIT") or (v["status"] == "UNKNOWN" and k in checks.CRITICAL)]
        p["status"] = "blocked: " + "; ".join(blockers) if blockers else "ok"

    buyable = [p for p in positions if p["status"] == "ok"]
    invested = sum(p["cost"] for p in buyable)
    feasible = len(positions) >= MIN_AFFORDABLE_SHARE * len(w)
    etf, etf_name = INDEX_FUND[market]
    etf_px = data.prices([etf], "1mo")["close"][etf].dropna()
    etf_price = float(etf_px.iloc[-1]) if len(etf_px) else None
    equity_budget = amount * reg["max_equity_exposure"]
    odds = horizon_odds(market, months, target_monthly_return)
    validation = funnel.latest_validation(market)

    notes = []
    if not feasible:
        notes.append(f"{amount:,.0f} is too small to hold the funnel's {len(w)}-stock portfolio: only {len(positions)} "
                     f"positions can buy even one share. Buying a handful of stocks instead throws away the "
                     f"diversification the strategy depends on.")
    if months < 12:
        notes.append(f"The strategy is built and tested for holding periods of a year or more; {months} months is "
                     "mostly market noise." + (" In India, selling within 12 months is also taxed at 20% (vs 12.5%)."
                                               if market == "IN" else ""))
    if reg["max_equity_exposure"] < 1:
        notes.append(f"The market is risk-off: the system keeps {1 - reg['max_equity_exposure']:.0%} of the money in cash "
                     "or a liquid fund until the trend recovers.")
    if target_monthly_return is not None:
        notes.append(f"No system can promise {target_monthly_return:.1%} a month. Historically, the {odds['index']} index "
                     f"reached {odds['target_total_return']:.1%} over {months} months in "
                     f"{odds['chance_of_reaching_target']:.0%} of periods, and lost money in {odds['chance_of_loss']:.0%}.")

    return {
        "amount": amount, "market": market, "universe": universe, "as_of": a["as_of"], "regime": reg["label"],
        "recommended_route": "stock portfolio" if feasible and validation.get("status") == "PASSED" else "index fund",
        "route_reason": ("the amount can hold the diversified book and the strategy has a validated edge here"
                         if feasible and validation.get("status") == "PASSED" else
                         "too little money to diversify across the funnel's picks" if not feasible else
                         "no validated edge in this market: the index fund is the evidence-based default"),
        "index_fund": {"ticker": etf, "name": etf_name, "unit_price": round(etf_price, 2) if etf_price else None,
                       "units_for_equity_budget": math.floor(equity_budget / etf_price) if etf_price else None,
                       "equity_budget": round(equity_budget, 2)},
        "stock_portfolio": {"positions": positions, "unaffordable": unaffordable, "invested_now": round(invested, 2),
                            "cash_or_liquid_fund": round(amount - invested, 2)},
        "historical_odds": odds,
        "evidence": validation.get("status"),
        "notes": notes,
        "disclaimer": "Historical frequencies, not forecasts. Not personal financial advice.",
    }
