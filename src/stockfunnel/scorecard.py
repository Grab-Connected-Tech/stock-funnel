"""Track record: grade every past recommendation against the market, out of sample.

This is the only legitimate way to tune the engine later: by its live results, never by re-fitting
the backtest. Recommendations are read from the hash-chained journal, so they can't be edited after
the fact.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from . import data, journal

HORIZONS = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}
# What a correct call looks like: BUY-type should beat the market; SELL/AVOID should lag it.
WANT = {"BUY": 1, "BUY AFTER": 1, "HOLD": 1, "SELL": -1, "AVOID": -1}


def log(market: str, ticker: str, as_of: str, action: str, price: float | None, source: str, conviction=None,
        signals: dict | None = None):
    """signals: {name: status}, kept so each hidden signal can later be graded on its own."""
    if action in WANT and price:
        journal.record("recommendation", {"market": market, "ticker": ticker, "as_of": as_of, "action": action,
                                          "price": price, "source": source, "conviction": conviction, "signals": signals})


def grade() -> dict:
    recs = {}
    for e in journal.entries("recommendation"):
        p = e["payload"]
        recs.setdefault((p["ticker"], p["as_of"]), p)  # first call per stock per day counts; no re-rolls
    if not recs:
        return {"note": "No recommendations logged yet. Each `analyze` and `run` adds to the record."}
    rows = []
    for market in ("IN", "US"):
        mine = [p for p in recs.values() if p["market"] == market]
        if not mine:
            continue
        bench = data.MARKETS[market]["benchmark"]
        px = data.prices(sorted({p["ticker"] for p in mine}) + [bench], period="2y")["close"].ffill()
        for p in mine:
            if p["ticker"] not in px:
                continue
            start = px.index.searchsorted(pd.Timestamp(p["as_of"]).tz_localize(px.index.tz), side="right") - 1
            row = {"market": market, "action": p["action"], "ticker": p["ticker"], "as_of": p["as_of"],
                   **{f"sig:{k}={v}": 1 for k, v in (p.get("signals") or {}).items() if v not in ("NEUTRAL", "UNKNOWN")}}
            for h, n in HORIZONS.items():
                if start >= 0 and start + n < len(px):
                    s, b = px[p["ticker"]], px[bench]
                    row[h] = (s.iloc[start + n] / s.iloc[start] - 1) - (b.iloc[start + n] / b.iloc[start] - 1)
            rows.append(row)
    df = pd.DataFrame(rows)
    out = {"recommendations": len(df), "as_of": date.today().isoformat(), "by_action": {}}
    for action, g in df.groupby("action"):
        stats = {"count": len(g)}
        for h in HORIZONS:
            if h in g and g[h].notna().any():
                x = g[h].dropna()
                stats[h] = {"graded": len(x), "avg_excess_vs_market": round(float(x.mean()), 4),
                            "hit_rate": round(float((x * WANT[action] > 0).mean()), 3)}
        out["by_action"][action] = stats
    sig_cols = [c for c in df.columns if c.startswith("sig:")]
    out["by_signal"] = {}
    for c in sig_cols:
        g = df[df[c] == 1]
        stats = {"count": len(g)}
        for h in HORIZONS:
            if h in g and g[h].notna().any():
                x = g[h].dropna()
                stats[h] = {"graded": len(x), "avg_excess_vs_market": round(float(x.mean()), 4)}
        out["by_signal"][c[4:]] = stats
    out["note"] = ("hit_rate = share of calls that went the right way vs the market (BUY/HOLD beat it, "
                   "SELL/AVOID lagged it). Needs ~30+ graded calls per action before it means anything.")
    return out
