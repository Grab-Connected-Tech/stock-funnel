"""Validation: walk-forward backtest of the *price-based* part of the funnel (momentum + low-vol +
lottery exclusion + rank buffer + trend regime), net of costs, with a deflated Sharpe ratio and
a ±20% parameter-perturbation test.

Honest limits (printed with every result):
- Survivorship bias: uses today's index members. Compare against the equal-weight universe
  (same bias) rather than the index, and treat absolute returns as inflated.
- Fundamental gates/themes are NOT backtested: free data isn't point-in-time. That needs a PIT
  source (SEC companyfacts `filed` dates for US, a paid vendor for India).
"""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import pandas as pd

from . import data
from .policy import PORTFOLIO, WEIGHTS

EULER = 0.5772156649


def deflated_sharpe(returns: pd.Series, trial_sharpes: list[float]) -> float:
    """Bailey & López de Prado (2014). Probability the true Sharpe > 0 after accounting for
    the number of variants tried. Per-period (non-annualized) Sharpe ratios."""
    r = returns.dropna()
    sr, t = r.mean() / r.std(), len(r)
    skew, kurt = float(r.skew()), float(r.kurt()) + 3  # pandas kurt is excess
    n = max(len(trial_sharpes), 2)
    nd = NormalDist()
    sr0 = math.sqrt(np.var(trial_sharpes)) * ((1 - EULER) * nd.inv_cdf(1 - 1 / n) + EULER * nd.inv_cdf(1 - 1 / (n * math.e)))
    denom = math.sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr ** 2, 1e-12))
    return nd.cdf((sr - sr0) * math.sqrt(t - 1) / denom)


def backtest(close: pd.DataFrame, bench: pd.Series, market: str, mom_lb: int = 252, n: int | None = None,
             buffer: int | None = None, vol_lb: int = 252, use_regime: bool = True) -> pd.Series:
    """Monthly-rebalanced net returns."""
    n = n or PORTFOLIO["n_holdings"][market]
    buffer = buffer or PORTFOLIO["hold_until_rank"][market]
    w_mom, w_lv = WEIGHTS[market]["momentum"], WEIGHTS[market]["low_vol"]
    cost = data.MARKETS[market]["cost_bps"] / 2 / 1e4
    month_ends = close.resample("ME").last().index
    daily = close.pct_change(fill_method=None)
    held: list[str] = []
    prev_w = pd.Series(dtype=float)
    out = {}
    for i in range(len(month_ends) - 1):
        t, nxt = month_ends[i], month_ends[i + 1]
        hist = close.loc[:t]
        if len(hist) < max(mom_lb, vol_lb) + 25:
            continue
        vol = daily.loc[:t].iloc[-vol_lb:].std() * np.sqrt(252)
        mom = hist.iloc[-22] / hist.iloc[-mom_lb] - 1
        ok = vol.notna() & mom.notna() & (vol.rank(pct=True) <= 0.9)
        z = lambda s: (s - s[ok].mean()) / s[ok].std()  # noqa: E731
        score = (w_mom * z(mom / vol) + w_lv * z(-vol))[ok]
        rank = score.rank(ascending=False)
        keep = [s for s in held if s in rank.index and rank[s] <= buffer]
        new = [s for s in rank.sort_values().index if s not in keep][: max(n - len(keep), 0)]
        held = keep + new
        iv = 1 / vol[held]
        w = (iv / iv.sum()).clip(upper=PORTFOLIO["max_weight"][market])
        w = w / w.sum()
        b = bench.loc[:t]
        sma10 = b.resample("ME").last().iloc[-10:].mean()
        w *= 1.0 if (not use_regime or b.iloc[-1] > sma10) else 0.5
        turnover = w.subtract(prev_w, fill_value=0).abs().sum()
        period = close.loc[t:nxt].iloc[[0, -1]]
        r = (period.iloc[-1] / period.iloc[0] - 1)[held].fillna(0)
        out[nxt] = float((w * r).sum() - turnover * cost)
        prev_w = w * (1 + r) / (1 + (w * r).sum())  # drift
    return pd.Series(out)


def _stats(r: pd.Series) -> dict:
    eq = (1 + r).cumprod()
    yrs = len(r) / 12
    return {"cagr": round(float(eq.iloc[-1] ** (1 / yrs) - 1), 4), "vol": round(float(r.std() * math.sqrt(12)), 4),
            "sharpe": round(float(r.mean() / r.std() * math.sqrt(12)), 3),
            "max_drawdown": round(float((eq / eq.cummax() - 1).min()), 4), "months": len(r)}


def run(universe: str, period: str = "20y") -> dict:
    market, tickers = data.universe(universe)
    bench_t = data.MARKETS[market]["benchmark"]
    px = data.prices(tickers + [bench_t], period=period)["close"]
    bench = px.pop(bench_t).dropna()
    close = px.dropna(axis=1, how="all")

    base = backtest(close, bench, market)
    ew = close.resample("ME").last().pct_change(fill_method=None).mean(axis=1).reindex(base.index)
    idx = bench.resample("ME").last().pct_change().reindex(base.index)

    # ±20% perturbations of every price parameter; each one is a "trial" for the DSR.
    variants = {"base": base}
    for name, kw in {"mom_lb-20%": {"mom_lb": 202}, "mom_lb+20%": {"mom_lb": 302},
                     "n-20%": {"n": 24}, "n+20%": {"n": 36}, "vol_lb-20%": {"vol_lb": 202},
                     "buffer-20%": {"buffer": int(PORTFOLIO["hold_until_rank"][market] * 0.8)},
                     "no_regime": {"use_regime": False}}.items():
        variants[name] = backtest(close, bench, market, **kw)
    sr = lambda r: float(r.mean() / r.std() * math.sqrt(12))  # noqa: E731
    # Compare risk-adjusted: the strategy deliberately runs below market volatility.
    edge = {k: round(sr(v) - sr(ew.reindex(v.index)), 3) for k, v in variants.items()}
    sharpes = [float(v.mean() / v.std()) for v in variants.values()]

    thirds = np.array_split(base.index, 3)
    sub = [round(sr(base[i]) - sr(ew[i]), 3) for i in thirds]
    dsr = deflated_sharpe(base, sharpes)
    verdict = {
        "dsr>=0.95": dsr >= 0.95,
        "sharpe_edge_vs_equal_weight_in_2of3_subperiods": sum(s > 0 for s in sub) >= 2,
        "all_perturbations_keep_sharpe_edge": all(v > 0 for v in edge.values()),
    }
    return {
        "universe": universe, "market": market, "period": f"{base.index[0].date()} → {base.index[-1].date()}",
        "strategy": _stats(base), "equal_weight_universe": _stats(ew.dropna()), "index_price_only": _stats(idx.dropna()),
        "sharpe_edge_vs_equal_weight": edge,
        "subperiod_sharpe_edge_vs_equal_weight": sub,
        "deflated_sharpe": round(dsr, 3), "trials": len(sharpes),
        "checks": verdict, "passed": all(verdict.values()),
        "caveats": ["survivorship bias: current constituents only (inflates all absolute numbers)",
                    "fundamental gates/themes not backtested (no point-in-time data)",
                    "index benchmark is price-only (excludes dividends)"],
    }
