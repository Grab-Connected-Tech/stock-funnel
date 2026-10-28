"""The funnel, end to end:

  1 Universe -> 2 Regime -> 3 Tradability prefilter -> 4 Forensic & solvency gates
  -> 5 Multi-factor score -> 6 Portfolio rules -> 7 LLM/human review (veto-only) -> 8 Execute (autopilot) / report (manual)

Everything up to step 6 is deterministic code. Step 7 can only block. Step 8 re-checks risk.
"""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from . import broker, data, gates, journal, metrics, portfolio, regime, review, score
from .policy import GATES

REVIEW_DIR = Path("reviews")
HIDDEN_FROM_REVIEW = {"analyst_rec_mean", "price", "avg_cost"}  # anchoring / herding inputs


def _fund_metrics(t: str) -> dict:
    for attempt in range(3):
        try:
            return metrics.fundamental_metrics(data.fundamentals(t))
        except Exception as e:  # yfinance rate limits / missing data
            if "Rate" in type(e).__name__ or "429" in str(e):
                time.sleep(10 * (attempt + 1))
                continue
            return {"data_error": str(e)[:120]}
    return {"data_error": "rate limited"}


def analyze_universe(universe: str, limit: int | None = None) -> dict:
    market, tickers = data.universe(universe)
    tickers = tickers[:limit] if limit else tickers
    reg = regime.assess(market)
    px = data.prices(tickers, period="2y")
    close, vol = px["close"], px["volume"]
    last_date = close.index[-1].date()

    rows = {}
    for t in tickers:
        if t not in close:
            rows[t] = {"history_days": 0}
            continue
        rows[t] = metrics.price_metrics(close[t], vol[t])
    df = pd.DataFrame.from_dict(rows, orient="index")
    df["vol_percentile"] = df["vol_1y"].rank(pct=True) if "vol_1y" in df else np.nan

    # Cheap price-only prefilter before spending API calls on fundamentals.
    pre = (df["history_days"] >= GATES["min_history_days"]) \
        & (df.get("price", 0) >= GATES["min_price"][market]) \
        & (df.get("adv_value_60d", 0) >= GATES["min_adv_value"][market])
    fund = {t: _fund_metrics(t) for t in df.index[pre.fillna(False)]}
    if fund:
        df = df.join(pd.DataFrame.from_dict(fund, orient="index"), how="left")

    results, rejected = {}, {}
    for t, row in df.iterrows():
        m = {k: (None if isinstance(v, float) and np.isnan(v) else v) for k, v in row.to_dict().items()}
        g = gates.evaluate(m, market)
        results[t] = g
        if not g["passed"]:
            rejected[t] = g["failed"] + [f"{u}?" for u in g["unknown"]]

    passed = df.loc[[t for t in df.index if results[t]["passed"]]].copy()
    passed["is_financial"] = passed["is_financial"].astype(bool) if "is_financial" in passed else False
    scored = score.composite(passed, market, reg["momentum_crash_guard"]) if len(passed) else passed
    return {"market": market, "universe": universe, "as_of": str(last_date), "regime": reg,
            "metrics": df, "gates": results, "rejected": rejected, "scored": scored,
            "last_prices": close.iloc[-1].dropna().to_dict(), "last_date": last_date}


def review_packet(t: str, a: dict) -> dict:
    """Everything a reviewer may see about one ticker, as citable facts. No prices/P&L/analyst ratings."""
    m = a["metrics"].loc[t].to_dict()
    facts = {f"m.{k}": (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()
             if k not in HIDDEN_FROM_REVIEW and v is not None and not (isinstance(v, float) and np.isnan(v))}
    facts |= {f"g.{k}": v for k, v in a["gates"][t]["checks"].items()}
    if t in a["scored"].index:
        s = a["scored"].loc[t]
        facts |= {f"s.{c}": round(float(s[c]), 3) for c in s.index if c.startswith("theme_") and pd.notna(s[c])}
        facts["s.rank"] = int(s["rank"])
        facts["s.universe_size_after_gates"] = len(a["scored"])
    facts |= {f"r.{k}": v for k, v in a["regime"].items()}
    return {"ticker": t, "market": a["market"], "as_of": a["as_of"], "facts": facts,
            "manual_checks_required": gates.MANUAL_CHECKS[a["market"]],
            "instructions": review.INSTRUCTIONS, "schema": review.SCHEMA}


def load_reviews(market: str, as_of: str, t: str) -> list[dict]:
    p = REVIEW_DIR / market / as_of / f"{t}.json"
    if not p.exists():
        return []
    try:
        got = json.loads(p.read_text())
    except json.JSONDecodeError:
        return [{"invalid": True}]  # counts as an invalid (blocking) review
    return got if isinstance(got, list) else [got]


def save_review(market: str, as_of: str, t: str, rev: dict) -> list[str]:
    p = REVIEW_DIR / market / as_of / f"{t}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(p.read_text()) if p.exists() else []
    existing.append(rev)
    p.write_text(json.dumps(existing, indent=2))
    return existing


def run(universe: str, mode: str = "manual", capital: float | None = None, limit: int | None = None,
        require_review: bool = True, today: date | None = None) -> dict:
    assert mode in ("manual", "autopilot")
    today = today or date.today()
    a = analyze_universe(universe, limit)
    market, last = a["market"], a["last_prices"]
    state = broker.load(market, capital)
    risk = broker.risk_status(state, last)
    held = broker.weights(state, last)

    p = portfolio.plan(a["scored"], a["rejected"], market, a["regime"], held, state["watchlist"],
                       capital=risk["nav"], today=today)
    state["watchlist"] |= p["watchlist_additions"]

    final = {}
    for t, act in p["actions"].items():
        packet_needed = act["action"] == "BUY"
        reviews = load_reviews(market, a["as_of"], t) if packet_needed else []
        c = review.combine(act["action"], reviews, review_packet(t, a) if packet_needed else {}, held=t in held,
                           require_review_for_buy=require_review)
        final[t] = {**act, "quant_action": act["action"], **c}
        if c["action"] != "BUY" and act["action"] == "BUY":
            final[t]["target_weight"] = round(held.get(t, 0.0), 4)

    executions = []
    if mode == "autopilot":
        executions = broker.execute(state, final, last, a["last_date"], today)
    broker.save(state)

    result = {
        "as_of": a["as_of"], "mode": mode, "market": market, "universe": universe,
        "regime": a["regime"], "risk": risk,
        "funnel": {"universe": len(a["metrics"]), "passed_gates": len(a["scored"]),
                   "rejected": len(a["rejected"]), "top_rejection_reasons": _top_reasons(a["rejected"])},
        "actions": final, "executions": executions,
        "pending_reviews": sorted(t for t, f in final.items() if f.get("pending_review")),
        "manual_checks_required": gates.MANUAL_CHECKS[market],
        "top_ranked": _top_table(a["scored"], 15),
    }
    journal.record("run", {k: v for k, v in result.items() if k != "top_ranked"})
    return result


def _top_reasons(rejected: dict) -> dict:
    counts: dict[str, int] = {}
    for reasons in rejected.values():
        for r in reasons:
            counts[r] = counts.get(r, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1])[:8])


def _top_table(scored: pd.DataFrame, n: int) -> list[dict]:
    cols = ["rank", "name", "sector", "score"] + [c for c in scored.columns if c.startswith("theme_")]
    top = scored.head(n)[[c for c in cols if c in scored]]
    return [{"ticker": t, **{k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()}}
            for t, r in top.iterrows()]
