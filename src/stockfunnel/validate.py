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


def deflated_sharpe(returns: pd.Series, trial_sharpes: list[float], n_trials: int | None = None) -> float:
    """Bailey & López de Prado (2014). Probability the true Sharpe > 0 after accounting for
    the number of variants tried. Per-period (non-annualized) Sharpe ratios."""
    r = returns.dropna()
    sr, t = r.mean() / r.std(), len(r)
    skew, kurt = float(r.skew()), float(r.kurt()) + 3  # pandas kurt is excess
    n = max(n_trials or len(trial_sharpes), 2)
    nd = NormalDist()
    sr0 = math.sqrt(np.var(trial_sharpes)) * ((1 - EULER) * nd.inv_cdf(1 - 1 / n) + EULER * nd.inv_cdf(1 - 1 / (n * math.e)))
    denom = math.sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr ** 2, 1e-12))
    return nd.cdf((sr - sr0) * math.sqrt(t - 1) / denom)


def _month_ends(df: pd.DataFrame) -> pd.DatetimeIndex:
    """Actual last trading day of each month (calendar month-ends can fall on weekends)."""
    return df.index[~df.index.to_period("M").duplicated(keep="last")]


def cscv(variants: dict[str, pd.Series], benchmark: pd.Series, blocks: int = 16) -> dict:
    """Combinatorially symmetric cross-validation (Bailey, Borwein, López de Prado, Zhu 2017).
    - pbo: how often the in-sample best configuration ranks below median out of sample. Informative
      only when configurations genuinely differ; ours are ±20% perturbations of a design fixed in
      advance, so ~0.5 is expected and is NOT used as a gate.
    - p_no_edge: across all C(16,8) half-samples of history, how often the base strategy's Sharpe
      fails to beat the benchmark's. This is the gate: an edge that only exists in some
      stretches of history is not an edge."""
    from itertools import combinations
    m = pd.DataFrame(variants).join(benchmark.rename("_bench"), how="inner").dropna()
    parts = np.array_split(np.arange(len(m)), blocks)
    sr = lambda x: x.mean() / x.std()  # noqa: E731
    configs = [c for c in m.columns if c != "_bench"]
    below = no_edge = total = 0
    for train in combinations(range(blocks), blocks // 2):
        tr = np.concatenate([parts[i] for i in train])
        te = np.concatenate([parts[i] for i in range(blocks) if i not in train])
        best = sr(m.iloc[tr][configs]).idxmax()
        oos = sr(m.iloc[te])
        below += oos[configs].rank()[best] / (len(configs) + 1) < 0.5
        no_edge += oos["base"] <= oos["_bench"]
        total += 1
    return {"pbo": below / total, "p_no_edge": no_edge / total}


def backtest(close: pd.DataFrame, bench: pd.Series, market: str, mom_lb: int = 252, n: int | None = None,
             buffer: int | None = None, vol_lb: int = 252, use_regime: bool = True) -> pd.Series:
    """Monthly-rebalanced net returns."""
    n = n or PORTFOLIO["n_holdings"][market]
    buffer = buffer or PORTFOLIO["hold_until_rank"][market]
    w_mom, w_lv = WEIGHTS[market]["momentum"], WEIGHTS[market]["low_vol"]
    cost = data.MARKETS[market]["cost_bps"] / 2 / 1e4
    month_ends = _month_ends(close)
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
        sma10 = b.loc[b.index.isin(month_ends)].iloc[-10:].mean()
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
    from . import journal
    market, tickers = data.universe(universe)
    bench_t = data.MARKETS[market]["benchmark"]
    px = data.prices(tickers + [bench_t], period=period)["close"].dropna(how="all")
    bench = px.pop(bench_t).dropna()
    close, _ = data.clean(px.dropna(axis=1, how="all"))

    base = backtest(close, bench, market)
    me = _month_ends(close)
    ew = close.loc[me].pct_change(fill_method=None).mean(axis=1).reindex(base.index)
    idx = bench.loc[bench.index.isin(me)].pct_change().reindex(base.index)

    # Symmetric ±20% perturbations of every price parameter; each is a trial for DSR and PBO.
    hold = PORTFOLIO["hold_until_rank"][market]
    variants = {"base": base}
    for name, kw in {"mom_lb-20%": {"mom_lb": 202}, "mom_lb+20%": {"mom_lb": 302},
                     "n-20%": {"n": 24}, "n+20%": {"n": 36}, "vol_lb-20%": {"vol_lb": 202}, "vol_lb+20%": {"vol_lb": 302},
                     "buffer-20%": {"buffer": int(hold * 0.8)}, "buffer+20%": {"buffer": int(hold * 1.2)},
                     "no_regime": {"use_regime": False}}.items():
        variants[name] = backtest(close, bench, market, **kw)
    common = pd.concat(variants, axis=1).dropna().index  # same window for every variant
    variants = {k: v.reindex(common) for k, v in variants.items()}
    base = variants["base"]

    sr = lambda r: float(r.mean() / r.std() * math.sqrt(12))  # noqa: E731
    # Compare risk-adjusted: the strategy deliberately runs below market volatility.
    edge = {k: round(sr(v) - sr(ew.reindex(v.index)), 3) for k, v in variants.items()}
    sharpes = [float(v.mean() / v.std()) for v in variants.values()]
    # Honest trial count: this run's variants + every earlier validation of this universe.
    prior = sum(1 for e in journal.entries("validation") if e["payload"].get("universe") == universe)
    n_trials = len(sharpes) * (1 + prior)
    # ponytail: perturbations are highly correlated, so their Sharpe variance understates the
    # dispersion of genuinely different ideas. Floor it at an annual-SR std of 0.3 between ideas.
    # Upgrade path: log every distinct strategy idea tried and use their Sharpe dispersion.
    var_floor = (0.3 / math.sqrt(12)) ** 2
    trial_sharpes = sharpes if np.var(sharpes) >= var_floor else list(np.array([-1, 1]) * math.sqrt(var_floor) + np.mean(sharpes))

    thirds = np.array_split(base.index, 3)
    sub = [round(sr(base[i]) - sr(ew[i]), 3) for i in thirds]
    dsr = deflated_sharpe(base, trial_sharpes, n_trials)
    cv = cscv(variants, ew)
    verdict = {
        "dsr>=0.95": dsr >= 0.95,
        "edge_in_80pct_of_cscv_half_samples": cv["p_no_edge"] < 0.2,
        "sharpe_edge_vs_equal_weight_in_2of3_subperiods": sum(s > 0 for s in sub) >= 2,
        "all_perturbations_keep_sharpe_edge": all(v > 0 for v in edge.values()),
    }
    return {
        "universe": universe, "market": market, "period": f"{base.index[0].date()} → {base.index[-1].date()}",
        "strategy": _stats(base), "equal_weight_universe": _stats(ew.dropna()), "index_price_only": _stats(idx.dropna()),
        "sharpe_edge_vs_equal_weight": edge,
        "subperiod_sharpe_edge_vs_equal_weight": sub,
        "deflated_sharpe": round(dsr, 3), "trials": n_trials,
        "cscv": {k: round(v, 3) for k, v in cv.items()},
        "checks": {k: bool(v) for k, v in verdict.items()}, "passed": bool(all(verdict.values())),
        "caveats": ["bad prints (daily moves < -50% or > +100%) repaired as data errors",
                    "survivorship bias: current constituents only (inflates all absolute numbers)",
                    "fundamental gates/themes not backtested (no point-in-time data)",
                    "backtest omits sector caps, min weight and drift band used live",
                    "index benchmark is price-only (excludes dividends)"],
    }
