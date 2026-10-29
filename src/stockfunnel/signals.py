"""Hidden signals: footprints that real-world events leave in structured data, often before or
without headlines. Rules only, no news reading, no LLM.

Each signal returns {"status": ..., "detail": str}:
  FAIL      severe event: avoid (or sell if held)
  WAIT      something unexplained just happened: don't open a position yet
  WARN      softer red flag: listed as a risk, costs conviction
  POSITIVE  evidence-backed good clue: adds at most one conviction point in total
  NEUTRAL   nothing notable · UNKNOWN source unavailable (ignored: these are not safety gates)

Evidence bar: each rule follows published, replicated findings (see docs/research/). Selling by
insiders is deliberately ignored: it is mostly personal (taxes, houses) and not predictive.
"""
from __future__ import annotations

import json
import urllib.parse
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from .checks import SEC_UA, _check, _cik, _get, _nse_json
from .data import _cached

# Thresholds are fixed judgments set before looking at any stock, not fitted.
INSIDER_DAYS, EVENT_DAYS, SEVERE_DAYS = 90, 180, 365
REVISION_PCT = 0.05            # ±5% move in consensus EPS over 90 days
SECTOR_GAP_SD = 2.5            # 1-month return this many peer std-devs below its sector
VOLUME_SPIKE = 3.0             # busiest recent session vs 60-day median

# NSE announcement categories (the exchange's own labels) that are red flags.
NSE_SEVERE = {
    "Fraud/Default/Arrest", "Defaults on Payment of Interest/Principal", "Corporate Insolvency Resolution Process",
    "Reasons for Delayed/Non-submission of Financial Results", "Frauds/Default by employees",
    "Delay/default in the payment of fines/penalties/dues etc. to authority", "Suspension of Trading", "Delisting",
}
# Deliberately NOT flagged (checked against real filings: routine, no evidence of predictive power):
# "News Verification"/"Rumour Verification" (exchanges query big caps about media reports constantly),
# "Change in Auditors" (mandatory rotation; resignations are caught by checks.py), litigation disclosures.
NSE_WARN = {
    "Clarification - Financial Results", "Reply to Clarification- Financial results", "Disruption of Operations",
    "Strikes/Lockouts/Disturbances", "Closure of operations",
}
# Suspension/delisting notices often concern debentures or partly-paid series, not the listed shares.
NOT_THE_EQUITY = ("Debenture", "NCD", "partly paid", "Partly Paid", "Commercial Paper", "Bond", "Matur", "Warrant",
                  "Preference", "Rights Entitlement")
NSE_INSIDERS = ("Promoter", "Director", "Key Managerial", "KMP")


def _since(days: int) -> date:
    return date.today() - timedelta(days=days)


# ---------------- 1. insiders ----------------

def insiders(ticker: str) -> dict:
    if ticker.endswith((".NS", ".BO")):
        sym = ticker.rsplit(".", 1)[0]
        q = urllib.parse.urlencode({"index": "equities", "symbol": sym,
                                    "from_date": _since(INSIDER_DAYS).strftime("%d-%m-%Y"),
                                    "to_date": date.today().strftime("%d-%m-%Y")})
        rows = _cached(f"nse_pit_{sym}", lambda: _nse_json("corporates-pit?" + q).get("data", []), max_age_days=1)
        mine = [r for r in rows if any(k in str(r.get("personCategory", "")) for k in NSE_INSIDERS)]
        buyers = {r.get("acqName") for r in mine if r.get("acqMode") == "Market Purchase" and r.get("tdpTransactionType") == "Buy"}
        promoter_sold = sum(max(float(r.get("befAcqSharesPer") or 0) - float(r.get("afterAcqSharesPer") or 0), 0)
                            for r in mine if "Promoter" in str(r.get("personCategory")) and r.get("acqMode") == "Market Sale")
        if promoter_sold >= 1.0:  # dumping >= 1% of the company in the market within 90 days
            return {"status": "WARN", "detail": f"promoters sold {promoter_sold:.1f}% of the company in the market (90 days)"}
    else:
        import yfinance as yf
        df = _cached(f"yf_insiders_{ticker}", lambda: yf.Ticker(ticker).insider_transactions, max_age_days=1)
        if df is None or df.empty:
            return {"status": "NEUTRAL", "detail": "no insider transactions on record"}
        when = pd.to_datetime(df["Start Date"], errors="coerce")
        recent = df[when >= pd.Timestamp(_since(INSIDER_DAYS))]
        buyers = set(recent.loc[recent["Text"].astype(str).str.startswith("Purchase at price"), "Insider"])
    if len(buyers) >= 2:  # cluster buying: Cohen-Malloy-Pomorski (2012), Lakonishok & Lee (2001)
        return {"status": "POSITIVE", "detail": f"{len(buyers)} insiders bought shares in the open market (90 days)"}
    if len(buyers) == 1:
        return {"status": "NEUTRAL", "detail": "1 insider open-market purchase (90 days)"}
    return {"status": "NEUTRAL", "detail": "no open-market insider buying (90 days)"}


# ---------------- 2. corporate behaviour ----------------

LENDER_ROUTINE = {"Fraud/Default/Arrest", "Frauds/Default by employees"}  # banks report borrower frauds as victims


def events(ticker: str, is_financial: bool = False) -> dict:
    if ticker.endswith((".NS", ".BO")):
        sym = ticker.rsplit(".", 1)[0]
        q = urllib.parse.urlencode({"index": "equities", "symbol": sym, "from_date": _since(SEVERE_DAYS).strftime("%d-%m-%Y"),
                                    "to_date": date.today().strftime("%d-%m-%Y")})
        rows = _cached(f"nse_ann_all_{sym}", lambda: _nse_json("corporate-announcements?" + q), max_age_days=1) or []
        severe, warn = [], []
        for r in rows:
            try:
                d = datetime.strptime(str(r.get("an_dt", ""))[:11], "%d-%b-%Y").date()
            except ValueError:
                continue
            desc = str(r.get("desc", "")).strip()
            text = str(r.get("attchmntText", ""))
            cfo_exit = desc in ("Resignation of Director/KMP/SMP", "Resignation", "Cessation") and \
                ("Chief Financial Officer" in text or "CFO" in text)
            if desc in NSE_SEVERE and not (desc in ("Suspension of Trading", "Delisting")
                                            and any(k in text for k in NOT_THE_EQUITY)):
                if is_financial and desc in LENDER_ROUTINE:
                    if d >= _since(EVENT_DAYS):
                        warn.append(f"{desc}, as a lender ({d:%d %b %Y})")
                else:
                    severe.append(f"{desc} ({d:%d %b %Y})")
            elif (desc in NSE_WARN or cfo_exit) and d >= _since(EVENT_DAYS):
                warn.append(f"{'CFO resignation' if cfo_exit else desc} ({d:%d %b %Y})")
    else:
        cik = _cik(ticker)
        if not cik:
            return {"status": "UNKNOWN", "detail": "company not found in SEC EDGAR"}
        c10 = f"{cik:010d}"
        sub = _cached(f"sec_sub_{c10}", lambda: json.loads(_get(f"https://data.sec.gov/submissions/CIK{c10}.json", SEC_UA)),
                      max_age_days=7)
        rec = sub["filings"]["recent"]
        severe, warn = [], []
        for form, d, items in zip(rec["form"], rec["filingDate"], rec["items"]):
            if d < _since(SEVERE_DAYS).isoformat():
                continue
            if form in ("NT 10-K", "NT 10-Q"):  # late filing notice: Alford, Jones & Zmijewski (1994)
                severe.append(f"late-filing notice {form} ({d})")
            if form.startswith("8-K"):
                if "1.03" in items:
                    severe.append(f"bankruptcy filing, 8-K 1.03 ({d})")
                if "3.01" in items:
                    severe.append(f"delisting notice, 8-K 3.01 ({d})")
                if "2.06" in items and d >= _since(EVENT_DAYS).isoformat():
                    warn.append(f"material impairment, 8-K 2.06 ({d})")
        exits = [d for f, d, i in zip(rec["form"], rec["filingDate"], rec["items"])
                 if f.startswith("8-K") and "5.02" in i and d >= _since(SEVERE_DAYS).isoformat()]
        auditor = [d for f, d, i in zip(rec["form"], rec["filingDate"], rec["items"])
                   if f.startswith("8-K") and "4.01" in i and d >= _since(SEVERE_DAYS).isoformat()]
        if exits and auditor:  # an officer exit alone is routine; together with an auditor change it isn't
            warn.append(f"executive change ({exits[0]}) and auditor change ({auditor[0]}) within a year")
    if severe:
        return {"status": "FAIL", "detail": "; ".join(severe[:2])}
    if warn:
        warn = list(dict.fromkeys(warn))  # same event filed twice counts once
        return {"status": "WARN", "detail": "; ".join(warn[:2]) + (f" (+{len(warn) - 2} more)" if len(warn) > 2 else "")}
    return {"status": "NEUTRAL", "detail": "no red-flag corporate filings"}


# ---------------- 3. price tells ----------------

def price_tells(ticker: str, metrics: pd.DataFrame) -> dict:
    """Bad news often shows in price before it shows in print."""
    m = metrics.loc[ticker]
    spike, r5 = m.get("volume_spike_5d"), m.get("ret_5d")
    if spike is not None and r5 is not None and spike >= VOLUME_SPIKE and r5 < -0.05:
        return {"status": "WAIT", "detail": f"volume {spike:.1f}× normal while the price fell {abs(r5):.0%} this week"}
    peers = metrics[(metrics["sector"] == m.get("sector")) & metrics["ret_1m"].notna()]["ret_1m"]
    if len(peers) >= 5 and m.get("ret_1m") is not None:
        gap = (m["ret_1m"] - peers.median()) / (peers.std() or np.nan)
        if gap <= -SECTOR_GAP_SD and m["ret_1m"] < -0.10:
            return {"status": "WAIT", "detail": f"down {abs(m['ret_1m']):.0%} this month while its sector was "
                                                f"{peers.median():+.0%}: possible unreported stock-specific news"}
    return {"status": "NEUTRAL", "detail": "trading in line with its sector"}


# ---------------- 4. analyst revisions ----------------

def revisions(ticker: str) -> dict:
    """Chan, Jegadeesh & Lakonishok (1996): estimate revisions predict returns."""
    import yfinance as yf

    def fetch():
        t = yf.Ticker(ticker)
        return {"trend": t.eps_trend, "rev": t.eps_revisions}
    got = _cached(f"yf_estimates_{ticker}", fetch, max_age_days=1)
    trend, rev = got["trend"], got["rev"]
    if trend is None or trend.empty or rev is None or rev.empty:
        return {"status": "NEUTRAL", "detail": "no analyst coverage"}
    rows = [p for p in ("0y", "+1y") if p in trend.index]
    changes = [trend.at[p, "current"] / trend.at[p, "90daysAgo"] - 1 for p in rows
               if trend.at[p, "90daysAgo"] and trend.at[p, "90daysAgo"] > 0]
    ups = sum(rev.at[p, "upLast30days"] for p in rows if p in rev.index)
    downs = sum(rev.at[p, "downLast30days"] for p in rows if p in rev.index)
    if not changes or ups + downs < 3:
        return {"status": "NEUTRAL", "detail": "too few analyst revisions to read"}
    chg = float(np.mean(changes))
    detail = f"consensus EPS {chg:+.1%} over 90 days; {ups} up vs {downs} down revisions (30 days)"
    if chg <= -REVISION_PCT and downs > ups:
        return {"status": "WARN", "detail": "analysts cutting estimates: " + detail}
    if chg >= REVISION_PCT and ups > downs:
        return {"status": "POSITIVE", "detail": "analysts raising estimates: " + detail}
    return {"status": "NEUTRAL", "detail": detail}


def run(ticker: str, metrics: pd.DataFrame) -> dict[str, dict]:
    return {"insider_activity": _check(lambda: insiders(ticker)),
            "corporate_events": _check(lambda: events(ticker, bool(metrics.loc[ticker].get("is_financial")))),
            "price_behaviour": _check(lambda: price_tells(ticker, metrics)),
            "analyst_revisions": _check(lambda: revisions(ticker))}
