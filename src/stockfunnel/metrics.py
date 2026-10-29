"""Per-ticker raw metrics: everything the gates and factors need, in one flat dict.
Pure functions of cached data, so the same day always yields the same numbers."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import accounting as acc
from .accounting import item
from .policy import GATES

FINANCIAL_SECTORS = {"Financial Services", "Financials"}  # + REITs below; developers get full forensic gates


def price_metrics(close: pd.Series, volume: pd.Series) -> dict:
    c = close.dropna()
    if len(c) < 260:
        return {"history_days": len(c)}
    r = c.pct_change().dropna()
    v = volume.reindex(c.index).fillna(0)
    vol_1y = float(r[-252:].std() * np.sqrt(252))
    ret = lambda a, b: float(c.iloc[-1 - b] / c.iloc[-1 - a] - 1)  # noqa: E731  return from a days ago to b days ago
    peak = c[-252:].cummax()
    return {
        "history_days": len(c),
        "price": float(c.iloc[-1]),
        "adv_value_60d": float((c[-60:] * v[-60:]).median()),  # median: robust to block deals
        "zero_volume_days_60d": int((v[-60:] == 0).sum()),
        "vol_1y": vol_1y,
        "mom_12_1": ret(252, 21),   # Jegadeesh-Titman, skip last month (short-term reversal)
        "mom_6_1": ret(126, 21),
        # NSE Momentum 30 style: return / daily-return sigma (dampens momentum-crash exposure)
        "mom_12_1_risk_adj": ret(252, 21) / vol_1y if vol_1y else None,
        "mom_6_1_risk_adj": ret(126, 21) / vol_1y if vol_1y else None,
        "ret_1m": ret(21, 0),
        "max_dd_1y": float((c[-252:] / peak - 1).min()),
        "above_200dma": bool(c.iloc[-1] > c[-200:].mean()),
        "max_daily_move_1y": float(r[-252:].abs().max()),
        "ret_5d": ret(5, 0),
        # unusual activity: busiest of the last 5 sessions vs the 60-day median volume
        "volume_spike_5d": float(v[-5:].max() / v[-65:-5].median()) if v[-65:-5].median() > 0 else None,
    }


def fundamental_metrics(f: dict) -> dict:
    inc, bal, cf, info = f["income"], f["balance"], f["cashflow"], f["info"]
    sector = info.get("sector") or "Unknown"
    mcap = info.get("marketCap")
    rev, ni = item(inc, acc.REV), item(inc, acc.NI)
    # Valuation & profitability use the freshest trailing-12-month flows when 4 consecutive quarters
    # exist; forensic gates below keep using audited annual statements (consistent full-year periods).
    t12 = acc.ttm(f.get("income_q"), inc.columns.max() if inc is not None and not inc.empty else None)
    rev_v, ni_v = (t12["rev"], t12["ni"]) if t12 else (rev, ni)
    ta, eq = item(bal, acc.TA), item(bal, acc.EQUITY)
    debt = item(bal, ["Total Debt"])
    if debt is None:  # yfinance omits Total Debt for some firms; rebuild it, else leave unknown
        parts = [item(bal, n) for n in (acc.LTD, ["Current Debt", "Current Debt And Capital Lease Obligation"])]
        debt = None if all(p is None for p in parts) else sum(p or 0.0 for p in parts)
    cash = item(bal, ["Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"]) or 0.0
    ebit, cfo = item(inc, acc.EBIT), item(cf, acc.CFO)
    ebit_v = t12["ebit"] if t12 and t12["ebit"] is not None else ebit
    gp_v = t12["gp"] if t12 and t12["gp"] is not None else item(inc, acc.GP)
    capex = item(cf, ["Capital Expenditure"])
    fcf = item(cf, ["Free Cash Flow"]) or (None if None in (cfo, capex) else cfo + capex)
    interest = item(inc, ["Interest Expense", "Interest Expense Non Operating"])
    ev = None if mcap is None or debt is None else mcap + debt - cash
    invested = None if eq is None or debt is None else eq + debt - cash
    divs = -(item(cf, ["Cash Dividends Paid", "Common Stock Dividend Paid"]) or 0.0)
    buyback = -(item(cf, ["Repurchase Of Capital Stock", "Common Stock Payments"]) or 0.0)
    issuance = item(cf, ["Common Stock Issuance", "Issuance Of Capital Stock"]) or 0.0
    ta1 = item(bal, acc.TA, 1)
    revs = [item(inc, acc.REV, y) for y in range(4)]
    nis = [item(inc, acc.NI, y) for y in range(4)]
    f_score, f_known = acc.piotroski(inc, bal, cf)
    sh0, sh1 = item(bal, acc.SHARES), item(bal, acc.SHARES, 1)
    cfos = [item(cf, acc.CFO, y) for y in range(3)]
    tas = [item(bal, acc.TA, y) for y in range(4)]
    roas = [nis[y] / tas[y] for y in range(4) if nis[y] is not None and tas[y]]
    is_fin = sector in FINANCIAL_SECTORS
    is_reit = "REIT" in str(info.get("industry") or "")
    div = lambda a, b: None if a is None or not b else a / b  # noqa: E731

    # Normalized earnings (Graham): if current profit is far above its multi-year average, value and
    # quality use the average so peak-cycle profits don't masquerade as cheapness or quality. One-sided
    # on purpose: a trough never makes a stock look better than its current numbers.
    hist_ni = [x for x in nis if x is not None]
    hist_ebit = [x for x in (item(inc, acc.EBIT, y) for y in range(4)) if x is not None]
    norm_ni = float(np.mean(hist_ni)) if len(hist_ni) >= 3 else None
    earnings_vs_normal = div(ni_v, norm_ni) if norm_ni and norm_ni > 0 and ni_v else None
    peak = bool(earnings_vs_normal and earnings_vs_normal > GATES["peak_earnings_ratio"])
    if peak:
        ni_v = norm_ni
        ebit_v = float(np.mean(hist_ebit)) if len(hist_ebit) >= 3 else ebit_v

    # REIT funds from operations ~ NAREIT FFO: net income + real-estate depreciation, minus gains on
    # property sales (cash-flow reconciling lines carry gains with a negative sign, so they are added).
    # ponytail: approximates FFO from yfinance lines; upgrade path is reported FFO from 10-K supplements.
    ffo = None
    if is_reit and ni is not None:
        dep = item(cf, ["Depreciation And Amortization", "Depreciation Amortization Depletion"]) or item(inc, acc.DEP) or 0.0
        gains = sum(item(cf, n) or 0.0 for n in ("Operating Gains Losses", "Gain Loss On Sale Of Business",
                                                  "Gain Loss On Sale Of PPE"))
        ffo = ni + dep + gains
    return {
        "name": info.get("shortName") or info.get("longName"),
        "sector": sector,
        "industry": info.get("industry"),
        "is_financial": is_fin,
        "is_reit": is_reit,
        "earnings_vs_normal": round(earnings_vs_normal, 3) if earnings_vs_normal else None,
        "peak_cycle_earnings": peak,
        "market_cap": mcap,
        "currency": info.get("currency"),
        "years_of_statements": int(inc.shape[1]) if inc is not None and not inc.empty else 0,
        # quality
        "fundamentals_basis": f"TTM to {t12['period_end']:%b %Y}" if t12 else f"FY to {inc.columns.max():%b %Y}" if inc is not None and not inc.empty else None,
        "roe": div(ni_v, eq),
        "roa": div(ni_v, ta),
        "roic": div(ebit_v * 0.75 if ebit_v is not None else None, invested if invested and invested > 0 else None),
        "gross_profitability": div(gp_v, ta),  # Novy-Marx (2013)
        "cfo_to_ni": div(cfo, ni) if ni and ni > 0 else None,
        "accruals": acc.sloan_accruals(inc, bal, cf),
        "piotroski": f_score,
        "piotroski_signals": f_known,
        "earnings_variability": float(np.std(roas)) if len(roas) >= 3 else None,
        "montier_c": None if is_fin or is_reit else acc.montier_c(inc, bal, cf),
        "negative_cfo_years_3y": sum(1 for x in cfos if x is not None and x < 0) if sum(x is not None for x in cfos) >= 2 else None,
        "net_issuance_1y": sh0 / sh1 - 1 if sh0 and sh1 else None,
        "loss_years_4y": sum(1 for x in nis if x is not None and x < 0),
        "positive_fcf_years_4y": sum(1 for y in range(4) if (item(cf, ["Free Cash Flow"], y) or -1) > 0),
        # safety
        "debt_to_equity": div(debt, eq) if eq and eq > 0 else None,
        "negative_equity": None if eq is None else eq <= 0,
        # No reported interest only counts as "no burden" when debt is known to be negligible.
        "interest_coverage": div(ebit, abs(interest)) if interest else
        (99.0 if ebit and ebit > 0 and debt is not None and ta and debt < 0.05 * ta else None),
        "altman_z2": None if is_fin or is_reit else acc.altman_z2(bal, inc),
        "beneish_m": None if is_fin or is_reit else acc.beneish_m(inc, bal, cf),
        "ffo": ffo,
        "ffo_yield": div(ffo, mcap),
        "ffo_to_assets": div(ffo, ta),
        "net_debt_to_ebitda": div(None if debt is None else debt - cash, item(inc, ["Normalized EBITDA", "EBITDA"])),
        # value
        "earnings_yield": div(ebit_v, ev) if ev and ev > 0 else None,  # Greenblatt EBIT/EV
        "fcf_yield": div(fcf, mcap),
        "fcf_to_ev": div(fcf, ev) if ev and ev > 0 else None,
        "sales_to_ev": div(rev_v, ev) if ev and ev > 0 else None,
        "earnings_to_price": div(ni_v, mcap),
        "book_to_price": div(eq, mcap),
        "pe": info.get("trailingPE"),
        "shareholder_yield": div(divs + buyback - issuance, mcap),
        # growth / investment
        "asset_growth": div(ta, ta1) - 1 if ta and ta1 else None,  # Cooper-Gulen-Schill: high = bad
        "revenue_cagr_3y": (revs[0] / revs[3]) ** (1 / 3) - 1 if revs[0] and revs[3] and revs[0] > 0 and revs[3] > 0 else None,
        "revenue": rev_v,
        # India governance (yfinance exposes insider % as a rough promoter-holding proxy)
        "insider_pct": info.get("heldPercentInsiders"),
        "analyst_rec_mean": info.get("recommendationMean"),
    }
