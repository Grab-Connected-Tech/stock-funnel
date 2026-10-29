"""Forensic accounting scores from annual statements (yfinance line-item names).

Every function returns None when the inputs aren't available. Callers treat None as
UNKNOWN, never as a pass.
"""
from __future__ import annotations

import math

import pandas as pd


def item(df: pd.DataFrame, names: str | list[str], year: int = 0) -> float | None:
    """Value of the first matching line item for the year-th most recent column (0 = latest)."""
    if df is None or df.empty or year >= df.shape[1]:
        return None
    for n in [names] if isinstance(names, str) else names:
        if n in df.index:
            v = df.iloc[:, year].get(n)
            if v is not None and not (isinstance(v, float) and math.isnan(v)):
                return float(v)
    return None


def _div(a, b):
    return None if a is None or b in (None, 0) else a / b


REV = ["Total Revenue", "Operating Revenue"]
NI = ["Net Income From Continuing Operation Net Minority Interest", "Net Income", "Net Income Common Stockholders"]
CFO = ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"]
TA = "Total Assets"
CA, CL = "Current Assets", "Current Liabilities"
LTD = ["Long Term Debt", "Long Term Debt And Capital Lease Obligation"]
GP = "Gross Profit"
PPE = ["Net PPE", "Gross PPE"]
REC = ["Accounts Receivable", "Receivables"]
DEP = ["Reconciled Depreciation", "Depreciation And Amortization", "Depreciation Amortization Depletion", "Depreciation"]
SGA = ["Selling General And Administration", "Selling And Marketing Expense"]
SHARES = ["Ordinary Shares Number", "Share Issued"]
EQUITY = ["Stockholders Equity", "Common Stock Equity"]
EBIT = ["EBIT", "Operating Income"]


def ttm(income_q: pd.DataFrame | None, after) -> dict | None:
    """Trailing-twelve-month sums of the income-statement flows, from the 4 newest quarters.
    Only when they are 4 *consecutive* quarters (free data has gaps) and newer than the latest
    annual report; otherwise None and callers keep the annual figures."""
    if income_q is None or income_q.empty or income_q.shape[1] < 4:
        return None
    cols = sorted(income_q.columns, reverse=True)[:4]
    gaps = [(cols[i] - cols[i + 1]).days for i in range(3)]
    if not all(80 <= g <= 100 for g in gaps) or (after is not None and cols[0] <= after):
        return None
    q = income_q[cols]
    out = {}
    for key, names in {"rev": REV, "ni": NI, "ebit": EBIT, "gp": [GP]}.items():
        vals = [item(q, names, i) for i in range(4)]
        out[key] = sum(vals) if all(v is not None for v in vals) else None
    out["period_end"] = cols[0]
    return out if out["rev"] is not None and out["ni"] is not None else None


def piotroski(inc, bal, cf) -> tuple[int | None, int]:
    """Piotroski (2000) F-score. Returns (score, number_of_signals_computable).
    Score is scaled to 0-9 from the computable signals; None if fewer than 7 computable."""
    def y(df, n, t=0):
        return item(df, n, t)

    ta0, ta1 = y(bal, TA), y(bal, TA, 1)
    roa0, roa1 = _div(y(inc, NI), ta0), _div(y(inc, NI, 1), ta1)
    cfo0 = y(cf, CFO)
    lev0, lev1 = _div(y(bal, LTD) or 0.0, ta0), _div(y(bal, LTD, 1) or 0.0, ta1)
    cr0, cr1 = _div(y(bal, CA), y(bal, CL)), _div(y(bal, CA, 1), y(bal, CL, 1))
    sh0, sh1 = y(bal, SHARES), y(bal, SHARES, 1)
    gm0, gm1 = _div(y(inc, GP), y(inc, REV)), _div(y(inc, GP, 1), y(inc, REV, 1))
    at0, at1 = _div(y(inc, REV), ta0), _div(y(inc, REV, 1), ta1)

    signals = [
        None if roa0 is None else roa0 > 0,
        None if cfo0 is None else cfo0 > 0,
        None if None in (roa0, roa1) else roa0 > roa1,
        None if None in (cfo0, ta0, roa0) else cfo0 / ta0 > roa0,
        None if None in (lev0, lev1) else lev0 <= lev1,
        None if None in (cr0, cr1) else cr0 > cr1,
        None if None in (sh0, sh1) else sh0 <= sh1 * 1.005,  # tolerate ESOP noise
        None if None in (gm0, gm1) else gm0 > gm1,
        None if None in (at0, at1) else at0 > at1,
    ]
    known = [s for s in signals if s is not None]
    if len(known) < 7:
        return None, len(known)
    return round(sum(known) * 9 / len(known)), len(known)


def altman_z2(bal, inc) -> float | None:
    """Altman Z'' (1995) for non-manufacturers / emerging markets: <1.1 distress, >2.6 safe.
    Not meaningful for banks/insurers (caller must skip financials)."""
    ta = item(bal, TA)
    tl = item(bal, ["Total Liabilities Net Minority Interest"])
    wc = item(bal, "Working Capital")
    if wc is None and None not in (item(bal, CA), item(bal, CL)):
        wc = item(bal, CA) - item(bal, CL)
    re, ebit, eq = item(bal, "Retained Earnings"), item(inc, EBIT), item(bal, EQUITY)
    if None in (ta, tl, wc, re, ebit, eq) or ta == 0 or tl == 0:
        return None
    return 6.56 * wc / ta + 3.26 * re / ta + 6.72 * ebit / ta + 1.05 * eq / tl


def beneish_m(inc, bal, cf) -> float | None:
    """Beneish (1999) 8-variable M-score. > -1.78 flags likely earnings manipulation."""
    g = lambda df, n, t: item(df, n, t)  # noqa: E731
    s0, s1 = g(inc, REV, 0), g(inc, REV, 1)
    r0, r1 = g(bal, REC, 0), g(bal, REC, 1)
    gp0, gp1 = g(inc, GP, 0), g(inc, GP, 1)
    ca0, ca1 = g(bal, CA, 0), g(bal, CA, 1)
    p0, p1 = g(bal, PPE, 0), g(bal, PPE, 1)
    ta0, ta1 = g(bal, TA, 0), g(bal, TA, 1)
    d0, d1 = g(inc, DEP, 0) or g(cf, DEP, 0), g(inc, DEP, 1) or g(cf, DEP, 1)
    sga0, sga1 = g(inc, SGA, 0), g(inc, SGA, 1)
    cl0, cl1 = g(bal, CL, 0), g(bal, CL, 1)
    ltd0, ltd1 = g(bal, LTD, 0) or 0.0, g(bal, LTD, 1) or 0.0
    ni0, cfo0 = g(inc, NI, 0), g(cf, CFO, 0)
    need = (s0, s1, r0, r1, gp0, gp1, ca0, ca1, p0, p1, ta0, ta1, d0, d1, cl0, cl1, ni0, cfo0)
    if any(v is None for v in need) or 0 in (s0, s1, r1, gp0, ta0, ta1):
        return None
    try:
        dsri = (r0 / s0) / (r1 / s1)
        gmi = (gp1 / s1) / (gp0 / s0)
        aqi = (1 - (ca0 + p0) / ta0) / (1 - (ca1 + p1) / ta1)
        sgi = s0 / s1
        depi = (d1 / (d1 + p1)) / (d0 / (d0 + p0))
        sgai = ((sga0 / s0) / (sga1 / s1)) if sga0 and sga1 else 1.0  # neutral if not reported
        lvgi = ((cl0 + ltd0) / ta0) / ((cl1 + ltd1) / ta1)
        tata = (ni0 - cfo0) / ta0
    except ZeroDivisionError:
        return None
    return (-4.84 + 0.920 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi
            + 0.115 * depi - 0.172 * sgai + 4.679 * tata - 0.327 * lvgi)


def montier_c(inc, bal, cf) -> int | None:
    """Montier (2008) C-score, 0-6 'cooking the books' signals. >= 5 is a red flag."""
    g = lambda df, n, t: item(df, n, t)  # noqa: E731
    s0, s1 = g(inc, REV, 0), g(inc, REV, 1)
    ni0, ni1, cfo0, cfo1 = g(inc, NI, 0), g(inc, NI, 1), g(cf, CFO, 0), g(cf, CFO, 1)
    if None in (s0, s1) or 0 in (s0, s1):
        return None
    r0, r1 = g(bal, REC, 0), g(bal, REC, 1)
    inv0, inv1 = g(bal, "Inventory", 0), g(bal, "Inventory", 1)
    oca0, oca1 = g(bal, "Other Current Assets", 0), g(bal, "Other Current Assets", 1)
    d0, d1 = g(inc, DEP, 0) or g(cf, DEP, 0), g(inc, DEP, 1) or g(cf, DEP, 1)
    gp0, gp1 = g(bal, "Gross PPE", 0), g(bal, "Gross PPE", 1)
    ta0, ta1 = g(bal, TA, 0), g(bal, TA, 1)
    def rising(a0, a1, scale0, scale1):
        """Ratio rose >5% relative, and the item is material (>= 2% of revenue). Avoids
        flagging noise in tiny line items (e.g. an IT firm's inventory)."""
        if None in (a0, a1) or 0 in (scale0, scale1) or a0 < 0.02 * s0 or a1 <= 0:
            return None if None in (a0, a1) else False
        return (a0 / scale0) > 1.05 * (a1 / scale1)

    signals = [
        None if None in (ni0, ni1, cfo0, cfo1) else (ni0 - cfo0) - (ni1 - cfo1) > 0.01 * s0,
        rising(r0, r1, s0, s1),
        rising(inv0, inv1, s0, s1),
        rising(oca0, oca1, s0, s1),
        None if None in (d0, d1, gp0, gp1) or 0 in (gp0, gp1) else d0 / gp0 < 0.95 * d1 / gp1,
        None if None in (ta0, ta1) or ta1 == 0 else ta0 / ta1 - 1 > 0.10,
    ]
    known = [s for s in signals if s is not None]
    return sum(known) if len(known) >= 4 else None


def sloan_accruals(inc, bal, cf) -> float | None:
    """(Net income - CFO) / average total assets. High accruals predict lower returns (Sloan 1996)."""
    ni, cfo, ta0, ta1 = item(inc, NI), item(cf, CFO), item(bal, TA), item(bal, TA, 1)
    if None in (ni, cfo, ta0):
        return None
    return (ni - cfo) / ((ta0 + (ta1 or ta0)) / 2)
