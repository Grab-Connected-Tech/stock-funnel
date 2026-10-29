"""Point-in-time replay: rewind to a past date, let the funnel see only data that existed then, and grade
its picks against what actually happened afterwards.

Deliberately SEPARATE from the live system: results go to replays/ only. Nothing here writes to the
journal, scorecard, paper book or watchlist, so hindsight experiments can never mix with the real,
recorded-in-advance track record.

What is rewound: prices, volume, volatility, momentum, regime, market cap, and financial statements
(a quarter or year only counts once its legal reporting deadline has passed).
What is switched off (no history available): today's surveillance lists, pledges, revision counts,
forward estimates, filings checks. So this tests the core funnel: data quality, safety gates,
scoring (with normalized earnings) and regime.
Known biases: today's index members (mild survivorship) and Yahoo's latest restated statements.
"""
from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from . import data, gates, metrics, regime, score
from .policy import GATES, PORTFOLIO

OUT = Path("replays")
LAG = {"IN": {"annual": 60, "quarter": 45}, "US": {"annual": 75, "quarter": 45}}  # legal reporting deadlines (days)


def _pit_fundamentals(f: dict, as_of: pd.Timestamp, market: str, price_ratio: float | None) -> dict:
    """The statements as they could have been known on as_of (published by then)."""
    g = copy.copy(f)
    ann = timedelta(days=LAG[market]["annual"])
    qtr = timedelta(days=LAG[market]["quarter"])
    for k in ("income", "balance", "cashflow"):
        df = f.get(k)
        if df is not None and not df.empty:
            g[k] = df[[c for c in df.columns if c + ann <= as_of]]
    q = f.get("income_q")
    if q is not None and not q.empty:
        g["income_q"] = q[[c for c in q.columns if c + qtr <= as_of]]
    info = dict(f.get("info") or {})
    if info.get("marketCap") and price_ratio:
        info["marketCap"] = info["marketCap"] * price_ratio  # same shares, price as of then
    for k in ("forwardEps", "trailingEps", "trailingPE", "recommendationMean"):  # no history: off
        info.pop(k, None)
    g["info"] = info
    return g


HIST_SP500 = ("https://raw.githubusercontent.com/fja05680/sp500/master/"
              "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv")


def sp500_members(as_of: str) -> list[str]:
    """S&P 500 members as they were on `as_of` (public historical-constituents dataset, Yahoo-style symbols).
    Removes the survivorship bias of replaying with today's members."""
    import io
    import urllib.request

    def fetch():
        raw = urllib.request.urlopen(urllib.request.Request(HIST_SP500, headers={"User-Agent": "stockfunnel"}), timeout=60).read()
        return pd.read_csv(io.BytesIO(raw))
    hist = data._cached("sp500_hist_members", fetch, max_age_days=7)
    row = hist[pd.to_datetime(hist["date"]) <= pd.Timestamp(as_of)].iloc[-1]
    return [t.strip().replace(".", "-") for t in row["tickers"].split(",") if t.strip()]


def analyze_as_of(universe: str, as_of: str, tickers: list[str] | None = None, px: dict | None = None) -> dict:
    """The funnel's core ranking as it would have been computed on `as_of`.
    tickers/px: override the universe (e.g. historical members) and pass pre-loaded prices."""
    market, current = data.universe(universe)
    tickers = tickers or current
    bench = data.MARKETS[market]["benchmark"]
    px = px or data.prices(tickers + [bench], period="20y")  # same cache as `validate`
    close_all, vol_all = px["close"].dropna(how="all"), px["volume"]
    ts = pd.Timestamp(as_of)
    idx = close_all.index.tz_localize(None) if close_all.index.tz is not None else close_all.index
    close, vol = close_all[idx <= ts], vol_all.reindex(close_all.index)[idx <= ts]
    now_close = data.last_prices(close_all)[0]
    then_close = data.last_prices(close)[0]
    clean, suspect = data.clean(close)

    rows = {}
    for t in tickers:
        if t not in clean or clean[t].dropna().empty:
            rows[t] = {"history_days": 0}
            continue
        rows[t] = metrics.price_metrics(clean[t], vol[t]) | {"bad_print_days_1y": int(suspect[t].iloc[-252:].sum())}
    df = pd.DataFrame.from_dict(rows, orient="index")
    df["vol_percentile"] = df["vol_1y"].rank(pct=True) if "vol_1y" in df else np.nan
    pre = (df["history_days"] >= GATES["min_history_days"]) & (df.get("price", 0) >= GATES["min_price"][market]) \
        & (df.get("adv_value_60d", 0) >= GATES["min_adv_value"][market])
    fund = {}
    for t in df.index[pre.fillna(False)]:
        try:
            ratio = then_close.get(t) / now_close[t] if now_close.get(t) else None
            fund[t] = metrics.fundamental_metrics(_pit_fundamentals(data.fundamentals(t), ts, market, ratio))
        except Exception as e:  # noqa: BLE001 - a broken record must not stop the replay
            fund[t] = {"data_error": str(e)[:80]}
    if fund:
        df = df.join(pd.DataFrame.from_dict(fund, orient="index"), how="left")

    res, rejected = {}, []
    for t, row in df.iterrows():
        m = {k: (None if isinstance(v, float) and np.isnan(v) else v) for k, v in row.to_dict().items()}
        res[t] = gates.evaluate(m, market)
        if not res[t]["passed"]:
            rejected.append(t)
    passed = df.loc[[t for t in df.index if res[t]["passed"]]].copy()
    for flag in ("is_financial", "is_reit"):
        passed[flag] = passed[flag].fillna(False).astype(bool) if flag in passed else False
    reg = regime.assess(market, as_of=ts)
    scored = score.composite(passed, market, reg["momentum_crash_guard"]) if len(passed) else passed
    return {"market": market, "universe": universe, "as_of": str(close.index[-1].date()), "regime": reg,
            "scored": scored, "rejected": rejected, "gate_results": res,
            "universe_tickers": [t for t in tickers if t in close_all and close_all[t].loc[:close.index[-1]].notna().any()],
            "requested": len(tickers), "close_all": close_all, "bench": bench}


def _fwd(close_all: pd.DataFrame, tickers: list[str], start: str, end: pd.Timestamp | None = None) -> pd.Series:
    """Return of each ticker from `start` to `end` (default: latest data)."""
    idx = close_all.index.tz_localize(None) if close_all.index.tz is not None else close_all.index
    s_i = idx.searchsorted(pd.Timestamp(start), side="right") - 1
    e_i = len(idx) - 1 if end is None else idx.searchsorted(end, side="right") - 1
    a = close_all.ffill().iloc[[s_i, e_i]]
    return (a.iloc[1] / a.iloc[0] - 1)[[t for t in tickers if t in a.columns]].dropna()


def top_picks(universe: str, months_back: int = 3, k: int = 5) -> dict:
    """The top-k picks as of `months_back` months ago, graded against what happened since."""
    close_all = data.prices(data.universe(universe)[1] + [data.MARKETS[data.universe(universe)[0]]["benchmark"]],
                            period="20y")["close"].dropna(how="all")
    latest = close_all.index[-1].tz_localize(None)
    as_of = (latest - pd.DateOffset(months=months_back)).date().isoformat()
    a = analyze_as_of(universe, as_of)
    sc, ca, bench = a["scored"], a["close_all"], a["bench"]
    ret_all = _fwd(ca, a["universe_tickers"], a["as_of"])
    ew = float(ret_all.mean())
    idx_ret = float(_fwd(ca, [bench], a["as_of"]).iloc[0])
    top = sc.head(k)
    top30 = sc.head(PORTFOLIO["n_holdings"][a["market"]]).index.tolist()
    picks = [{"rank": int(top.at[t, "rank"]), "ticker": t, "name": top.at[t, "name"] if "name" in top else None,
              "return_since": round(float(ret_all.get(t, np.nan)), 4),
              "vs_equal_weight": round(float(ret_all.get(t, np.nan)) - ew, 4),
              "beat_equal_weight": bool(ret_all.get(t, -9) > ew)} for t in top.index]
    avoid = [t for t in a["rejected"] if t in ret_all.index]
    return {
        "universe": universe, "as_of": a["as_of"], "graded_to": str(latest.date()),
        "regime_then": a["regime"]["label"], "max_equity_then": a["regime"]["max_equity_exposure"],
        "picks": picks,
        "top5_avg": round(float(ret_all.reindex(top.index).mean()), 4),
        "top30_avg": round(float(ret_all.reindex(top30).mean()), 4),
        "avoided_avg": round(float(ret_all.reindex(avoid).mean()), 4) if avoid else None,
        "equal_weight_universe": round(ew, 4), "index_price_only": round(idx_ret, 4),
        "ranked_then": len(sc), "avoided_then": len(avoid),
    }


def multi_date(universe: str, months: int = 24, horizon_months: int = 3, historical_members: bool | None = None) -> dict:
    """Repeat the replay at every month-end, grading each over `horizon_months`. One window is luck;
    many windows start to show whether the ranking itself carries information.
    historical_members: use the index as it was on each date (default: on for sp500, where the data exists)."""
    market, tickers = data.universe(universe)
    historical = (universe == "sp500") if historical_members is None else (historical_members and universe == "sp500")
    bench = data.MARKETS[market]["benchmark"]
    probe = data.prices([bench], period="20y")["close"].dropna(how="all")
    latest = probe.index[-1].tz_localize(None)
    dates = [(latest - pd.DateOffset(months=m)).normalize() for m in range(months, horizon_months - 1, -1)
             if (latest - pd.DateOffset(months=m)).normalize() + pd.DateOffset(months=horizon_months) <= latest]
    members = {d: (sp500_members(d.date().isoformat()) if historical else tickers) for d in dates}
    union = sorted(set().union(*members.values())) if members else tickers
    px = data.prices(union + [bench], period="20y")
    close_all = px["close"].dropna(how="all")
    rows, gate_rows, coverage = [], [], []
    for as_of in dates:
        end = as_of + pd.DateOffset(months=horizon_months)
        a = analyze_as_of(universe, as_of.date().isoformat(), tickers=members[as_of], px=px)
        coverage.append(len(a["universe_tickers"]) / max(a["requested"], 1))
        sc = a["scored"]
        if sc.empty:
            continue
        r = _fwd(close_all, a["universe_tickers"], a["as_of"], end)
        ew = float(r.mean())
        avoid = [t for t in a["rejected"] if t in r.index]
        for t in avoid:  # gate attribution: which gate rejected each stock, and how it then did vs the market
            g = a["gate_results"][t]
            reasons = g["failed"] or ["missing data: " + ", ".join(g["unknown"])]
            for reason in (reasons if g["failed"] else ["missing data only"]):
                gate_rows.append({"gate": reason, "excess": float(r[t] - ew)})
        rp = r.reindex(sc.index).dropna()                      # gate-passing stocks with a return
        ranked = sc.loc[rp.index]
        q = pd.qcut(ranked["rank"], 5, labels=False, duplicates="drop") if len(rp) >= 10 else None
        # rank correlation without scipy: Pearson on ranks. Higher score should mean higher return.
        ic = lambda col: float(ranked[col].rank().corr(rp.rank())) if col in ranked and ranked[col].notna().sum() > 5 else np.nan  # noqa: E731
        rows.append({"as_of": a["as_of"], "regime": a["regime"]["label"], "ranked": len(sc),
                     "top5_vs_ew": float(r.reindex(sc.head(5).index).mean() - ew),
                     "top30_vs_ew": float(r.reindex(sc.head(30).index).mean() - ew),
                     "avoided_vs_ew": float(r.reindex(avoid).mean() - ew) if avoid else np.nan,
                     "top30_vs_rest_passed": float(rp.reindex(sc.index[:30]).mean() - rp.reindex(sc.index[30:]).mean()),
                     "rank_ic": ic("score"),
                     **{f"ic_{c[6:]}": ic(c) for c in sc.columns if c.startswith("theme_")},
                     **({f"q{i + 1}": float(rp[q == i].mean()) for i in range(5)} if q is not None else {})})
    df = pd.DataFrame(rows)
    summary = {}
    for c in ("top5_vs_ew", "top30_vs_ew", "avoided_vs_ew"):
        x = df[c].dropna()
        good = (x > 0) if c != "avoided_vs_ew" else (x < 0)
        summary[c] = {"windows": len(x), "avg": round(float(x.mean()), 4), "median": round(float(x.median()), 4),
                      "share_right": round(float(good.mean()), 3)}
    # Does the ORDER carry information? Judged only among stocks that passed the gates, so it measures the
    # ranking, not the gates (or the gates' survivorship confound).
    t30 = df["top30_vs_rest_passed"].dropna()
    ics = df["rank_ic"].dropna()
    ranking = {
        "avg_rank_correlation": round(float(ics.mean()), 4),
        "share_windows_positive_correlation": round(float((ics > 0).mean()), 3),
        "top30_vs_rest_of_passed": {"avg": round(float(t30.mean()), 4), "share_right": round(float((t30 > 0).mean()), 3)},
        "avg_return_by_quintile": {f"Q{i + 1}{' (best ranked)' if i == 0 else ' (worst ranked)' if i == 4 else ''}":
                                   round(float(df[f"q{i + 1}"].mean()), 4) for i in range(5) if f"q{i + 1}" in df},
        "factor_rank_correlation": {c[3:]: round(float(df[c].mean()), 4) for c in df.columns if c.startswith("ic_")},
        "informative": bool(ics.mean() > 0 and (ics > 0).mean() >= 0.5 and (t30 > 0).mean() >= 0.5),
    }
    gdf = pd.DataFrame(gate_rows)
    gate_attribution = {} if gdf.empty else {
        gate: {"stock_windows": int(len(x)), "avg_excess_vs_ew": round(float(x.mean()), 4),
               "share_lagged_market": round(float((x < 0).mean()), 3)}
        for gate, x in gdf.groupby("gate")["excess"] if len(x) >= 5}
    gate_attribution = dict(sorted(gate_attribution.items(), key=lambda kv: -kv[1]["stock_windows"]))
    return {"universe": universe, "market": market, "horizon_months": horizon_months, "windows": len(df),
            "membership": "historical (index as it was on each date)" if historical else "today's members (survivorship-biased)",
            "price_coverage": round(float(np.mean(coverage)), 3) if coverage else None,
            "gate_attribution": gate_attribution,
            "first": df["as_of"].iloc[0] if len(df) else None, "last": df["as_of"].iloc[-1] if len(df) else None,
            "summary": summary, "ranking": ranking, "by_date": df.round(4).to_dict("records"),
            "caveats": ["windows overlap (monthly starts, 3-month horizon): fewer independent samples than it looks",
                        ("historical index membership; members Yahoo has no prices for (often delisted) are missing: "
                         "see price_coverage" if historical else "today's index members (survivorship bias)"),
                        "Yahoo's latest (restated) statements",
                        "live-only checks and signals off; only the core funnel is replayed",
                        "older dates have fewer published statements, so more stocks fail the gates fail-closed"]}


def save(name: str, obj: dict) -> Path:
    OUT.mkdir(exist_ok=True)
    p = OUT / f"{date.today().isoformat()}-{name}.json"
    p.write_text(json.dumps(obj, indent=2, default=str))
    return p


# ---------------- benchmarking API (used by MCP / CLI) ----------------

BENCHMARK_ETFS = {"US": {"SPY": "S&P 500 (SPY)", "RSP": "S&P 500 equal-weight (RSP)"},
                  "IN": {"NIFTYBEES.NS": "Nifty 50 (NIFTYBEES)", "JUNIORBEES.NS": "Nifty Next 50 (JUNIORBEES)"}}
MIN_RANKED_SHARE = 0.3  # below this share of the universe passing the gates, the window lacks the data to judge


def _framework_weights(a: dict) -> pd.Series:
    """The portfolio the framework would have held: top-N, inverse-vol, capped, regime-scaled (rest in cash)."""
    from .portfolio import capped_weights
    market, sc = a["market"], a["scored"]
    book = sc[sc["rank"] <= PORTFOLIO["n_holdings"][market]].dropna(subset=["vol_1y"])
    if book.empty:
        return pd.Series(dtype=float)
    w = capped_weights(1 / book["vol_1y"], book["sector"], PORTFOLIO["max_weight"][market], PORTFOLIO["max_sector_weight"])
    w = w[w >= PORTFOLIO["min_weight"]]
    return w / w.sum() * a["regime"]["max_equity_exposure"]


def benchmark(universe: str, months_back: list[int], compare_tickers: list[str] | None = None) -> dict:
    """For each look-back window: what the framework would have held then (data of that date only), and the
    return of that portfolio, its top 5, the equal-weight universe, benchmark ETFs and any caller-supplied
    tickers, all measured over the SAME period (start date -> latest data). Returns are decimals (0.05 = +5%)."""
    market, tickers = data.universe(universe)
    etfs = BENCHMARK_ETFS[market]
    compare = [t.strip().upper() for t in (compare_tickers or []) if t.strip()]
    close_all = data.prices(tickers + [data.MARKETS[market]["benchmark"]], period="20y")["close"].dropna(how="all")
    extra = data.prices(sorted(set(etfs) | set(compare)), period="20y")["close"].dropna(how="all")
    latest = close_all.index[-1].tz_localize(None)
    windows = []
    for m in sorted(set(int(x) for x in months_back)):
        as_of = (latest - pd.DateOffset(months=m)).normalize().date().isoformat()
        a = analyze_as_of(universe, as_of)
        sc = a["scored"]
        sufficient = len(sc) >= MIN_RANKED_SHARE * len(a["universe_tickers"])
        uni = _fwd(close_all, a["universe_tickers"], a["as_of"])
        ret = {"equal_weight_universe": round(float(uni.mean()), 4)}
        ref = _fwd(extra, list(etfs), a["as_of"])
        ret |= {etfs[t]: round(float(ref[t]), 4) for t in etfs if t in ref}
        w = _framework_weights(a) if sufficient else pd.Series(dtype=float)
        holdings, top5 = [], []
        if sufficient and not w.empty:
            r = _fwd(close_all, list(w.index), a["as_of"])
            ret["framework_portfolio"] = round(float((w.reindex(r.index).fillna(0) * r).sum()), 4)
            t5 = list(sc.index[:5])
            r5 = _fwd(close_all, t5, a["as_of"])
            ret["framework_top5_equal_weight"] = round(float(r5.mean()), 4)
            top5 = [{"rank": i + 1, "ticker": t, "name": sc.at[t, "name"] if "name" in sc else None,
                     "return": round(float(r5.get(t, np.nan)), 4)} for i, t in enumerate(t5)]
            holdings = [{"ticker": t, "weight": round(float(wt), 4), "return": round(float(r.get(t, np.nan)), 4)}
                        for t, wt in w.sort_values(ascending=False).items()]
        yours = {}
        if compare:
            rc = _fwd(extra, compare, a["as_of"])
            yours = {t: (round(float(rc[t]), 4) if t in rc else None) for t in compare}
            got = [v for v in yours.values() if v is not None]
            if got:
                ret["your_tickers_equal_weight"] = round(float(np.mean(got)), 4)
        fw = ret.get("framework_portfolio")
        windows.append({
            "window": f"{m} months", "as_of": a["as_of"], "graded_to": str(latest.date()),
            "data_sufficient": bool(sufficient),
            "data_note": None if sufficient else (f"only {len(sc)} of {len(a['universe_tickers'])} stocks had enough published "
                                                  "statements to pass the gates on this date; framework result omitted"),
            "regime_then": a["regime"]["label"], "equity_exposure_then": a["regime"]["max_equity_exposure"],
            "returns": ret,
            "framework_minus": ({k: round(fw - v, 4) for k, v in ret.items() if k != "framework_portfolio"} if fw is not None else None),
            "framework_top5": top5, "framework_holdings": holdings, "your_tickers": yours or None,
        })
    return {
        "universe": universe, "market": market, "units": "returns are decimals from as_of close to graded_to close",
        "method": "Hindsight replay: the funnel's core (data quality, safety gates, scoring, regime) re-run with only data "
                  "available on each as_of date; statements count once their legal reporting deadline has passed. "
                  "framework_portfolio = top 30 at inverse-volatility weights, scaled by the regime (cash earns 0), "
                  "held unchanged to graded_to. Benchmarks are real ETFs including dividends.",
        "windows": windows,
        "caveats": ["Hindsight test, NOT part of the live track record (see track_record for real, recorded-in-advance calls).",
                    "Uses today's index members: mild survivorship bias in the universe.",
                    "Uses the latest (restated) statements; live-only checks and signals are switched off.",
                    "Each window is a single start date: treat as anecdote; use replay_monthly for a steadier read.",
                    "Holding the portfolio unchanged ignores the framework's monthly rebalancing and sell rules."],
    }
