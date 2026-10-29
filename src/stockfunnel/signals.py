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

def _recent_results(ticker: str, as_of: date, days: int = 14) -> date | None:
    """Most recent results date within `days` before as_of (yfinance earnings history), else None."""
    import yfinance as yf
    try:
        idx = _cached(f"yf_earn_dates_{ticker}", lambda: list(yf.Ticker(ticker).get_earnings_dates(limit=8).index),
                      max_age_days=1)
    except Exception:  # noqa: BLE001 - this only refines the wording
        return None
    past = [d.date() for d in idx if as_of - timedelta(days=days) <= d.date() <= as_of]
    return max(past) if past else None


def price_tells(ticker: str, metrics: pd.DataFrame, as_of: date | None = None) -> dict:
    """Bad news often shows in price before it shows in print."""
    m = metrics.loc[ticker]
    spike, r5 = m.get("volume_spike_5d"), m.get("ret_5d")
    if spike is not None and r5 is not None and spike >= VOLUME_SPIKE and r5 < -0.05:
        res = _recent_results(ticker, as_of or date.today())
        why = (f"after results on {res:%d %b}: the market is still digesting the numbers" if res
               else "with no results in the last two weeks: possible unreported news")
        return {"status": "WAIT", "detail": f"volume {spike:.1f}× normal while the price fell {abs(r5):.0%} this week, {why}"}
    peers = metrics[(metrics["sector"] == m.get("sector")) & metrics["ret_1m"].notna()]["ret_1m"]
    if len(peers) >= 5 and m.get("ret_1m") is not None:
        gap = (m["ret_1m"] - peers.median()) / (peers.std() or np.nan)
        if gap <= -SECTOR_GAP_SD and m["ret_1m"] < -0.10:
            res = _recent_results(ticker, as_of or date.today(), days=31)
            why = f"after results on {res:%d %b}" if res else "possible unreported stock-specific news"
            return {"status": "WAIT", "detail": f"down {abs(m['ret_1m']):.0%} this month while its sector was "
                                                f"{peers.median():+.0%}: {why}"}
    return {"status": "NEUTRAL", "detail": "trading in line with its sector"}


# ---------------- 4. analyst revisions ----------------

def _estimates(ticker: str) -> dict:
    import yfinance as yf

    def fetch():
        t = yf.Ticker(ticker)
        return {"trend": t.eps_trend, "rev": t.eps_revisions, "est": t.earnings_estimate}
    return _cached(f"yf_estimates2_{ticker}", fetch, max_age_days=1)


def _annual_counts(got: dict) -> tuple[int, int, str | None]:
    """30-day up/down revision counts for the annual estimates, plus a reason if they can't be trusted.
    Missing data must never read as 'no revisions': the provider sometimes omits annual counts even
    while the estimates themselves moved."""
    rev, trend, est = got["rev"], got["trend"], got.get("est")
    rows = [p for p in ("0y", "+1y") if rev is not None and p in rev.index]
    ups = int(sum(rev.at[p, "upLast30days"] for p in rows)) if rows else 0
    downs = int(sum(rev.at[p, "downLast30days"] for p in rows)) if rows else 0
    analysts = int(est.at["0y", "numberOfAnalysts"]) if est is not None and not est.empty and "0y" in est.index \
        and pd.notna(est.at["0y", "numberOfAnalysts"]) else None
    coverage = f"{analysts} analyst{'s' if analysts != 1 else ''} in the data source" if analysts else "coverage unknown"
    if not rows:
        return ups, downs, f"revision data unavailable ({coverage})"
    moved = trend is not None and not trend.empty and "0y" in trend.index and \
        abs(trend.at["0y", "current"] - trend.at["0y", "30daysAgo"]) > 1e-9
    if ups + downs == 0 and moved:
        return ups, downs, f"revision counts unavailable: estimates moved but the provider reports none ({coverage})"
    return ups, downs, None if ups + downs else f"no revisions in 30 days ({coverage})"


def revisions(ticker: str) -> dict:
    """Chan, Jegadeesh & Lakonishok (1996): estimate revisions predict returns."""
    got = _estimates(ticker)
    trend = got["trend"]
    if trend is None or trend.empty:
        return {"status": "NEUTRAL", "detail": "no analyst coverage in the data source"}
    ups, downs, problem = _annual_counts(got)
    if problem and "unavailable" in problem:
        return {"status": "UNKNOWN", "detail": problem}
    rows = [p for p in ("0y", "+1y") if p in trend.index]
    changes = [trend.at[p, "current"] / trend.at[p, "90daysAgo"] - 1 for p in rows
               if trend.at[p, "90daysAgo"] and trend.at[p, "90daysAgo"] > 0]
    if not changes or ups + downs < 3:
        return {"status": "NEUTRAL", "detail": problem or f"too few revisions to read ({ups} up, {downs} down)"}
    chg = float(np.mean(changes))
    detail = f"consensus EPS {chg:+.1%} over 90 days; {ups} up vs {downs} down revisions (30 days)"
    if chg <= -REVISION_PCT and downs > ups:
        return {"status": "WARN", "detail": "analysts cutting estimates: " + detail}
    if chg >= REVISION_PCT and ups > downs:
        return {"status": "POSITIVE", "detail": "analysts raising estimates: " + detail}
    return {"status": "NEUTRAL", "detail": detail}


# ---------------- 5. results calendar ----------------

RESULTS_WINDOW_DAYS, AFTER_RESULTS_DAYS = 5, 3


def _last_quarter_end(d: date) -> date:
    q_month = ((d.month - 1) // 3) * 3  # 0, 3, 6, 9 -> quarter that ended before this one started
    return (date(d.year, q_month + 1, 1) - timedelta(days=1)) if q_month else date(d.year - 1, 12, 31)


def _reporting_deadline(quarter_end: date, india: bool) -> date:
    """Legal filing deadlines: India SEBI LODR 45 days (60 for the March/annual quarter);
    US 10-Q ~45 days, 10-K up to 75 days (December year-ends)."""
    annual = quarter_end.month == (3 if india else 12)
    return quarter_end + timedelta(days=(60 if india else 75) if annual else 45)


def upcoming_results(ticker: str, as_of: date | None = None) -> dict:
    """Prices jump on results day; never decide on numbers that are about to change, or already have.
    WAIT when: results came out after this data snapshot; results are due within a few days; or a quarter
    has ended, it isn't reported yet and we're inside the legal reporting window with no date published.
    The recommendation also moves a later buy date past a known results date (see recommend.py)."""
    import yfinance as yf

    from .data import fundamentals
    today, as_of = date.today(), as_of or date.today()
    cal = _cached(f"yf_calendar_{ticker}", lambda: yf.Ticker(ticker).calendar or {}, max_age_days=1)
    dates = sorted(d for d in (cal.get("Earnings Date") or []) if isinstance(d, date))
    q = fundamentals(ticker).get("income_q")
    latest_q = max(q.columns).date() if q is not None and not q.empty else None

    released = [d for d in dates if as_of < d <= today]
    if released:
        return {"status": "WAIT", "detail": f"results came out {released[-1]:%d %b %Y}, after this data snapshot "
                                            f"({as_of:%d %b}): recompute on the new numbers", "date": None}
    future = [d for d in dates if d > as_of]
    if future:
        d = future[0]
        if (d - as_of).days <= RESULTS_WINDOW_DAYS:
            return {"status": "WAIT", "detail": f"results due {d:%d %b %Y}: wait for the numbers", "date": d.isoformat()}
        return {"status": "NEUTRAL", "detail": f"next results {d:%d %b %Y}", "date": d.isoformat()}
    qe = _last_quarter_end(today)
    deadline = _reporting_deadline(qe, ticker.endswith((".NS", ".BO")))
    if latest_q and latest_q < qe and today <= deadline:
        return {"status": "WAIT", "detail": f"quarter ended {qe:%d %b} isn't reported yet (due by {deadline:%d %b}) "
                                            "and no date is published: wait for the numbers", "date": None}
    if latest_q and latest_q >= qe:
        return {"status": "NEUTRAL", "detail": f"latest quarter ({latest_q:%b %Y}) reported; next date not published yet",
                "date": None}
    return {"status": "NEUTRAL", "detail": "no scheduled results date found", "date": None}


# ---------------- 6. revision breadth (tracking only) ----------------

# Signals in SHADOW are computed, shown and graded by the scorecard, but never change a decision until
# their live record justifies it. Added after seeing examples it would flag, so it must earn its place.
SHADOW = {"revision_breadth", "forward_earnings"}
FORWARD_DROP = -0.15  # consensus next-year EPS this far below trailing EPS


def forward_earnings(ticker: str, metrics: pd.DataFrame) -> dict:
    """Consensus expects earnings to fall: trailing numbers may be a peak (tracking only)."""
    chg = metrics.loc[ticker].get("forward_eps_change")
    if chg is None or chg != chg:
        return {"status": "NEUTRAL", "detail": "no forward estimate in the data source"}
    used = bool(metrics.loc[ticker].get("valued_on_forward"))
    detail = (f"consensus forward EPS {chg:+.0%} vs trailing" +
              ("; valuation already uses the lower forward figure" if used else "") +
              " (this signal's conviction effect is tracking only)")
    return {"status": "WARN" if chg <= FORWARD_DROP else "NEUTRAL", "detail": detail}


def revision_breadth(ticker: str) -> dict:
    """Net breadth of 30-day estimate revisions, with a 2:1 majority rule."""
    got = _estimates(ticker)
    if got["rev"] is None or got["rev"].empty:
        return {"status": "NEUTRAL", "detail": "no analyst coverage in the data source"}
    ups, downs, problem = _annual_counts(got)
    if problem and "unavailable" in problem:
        return {"status": "UNKNOWN", "detail": problem}
    if ups + downs < 5:
        return {"status": "NEUTRAL", "detail": f"too few revisions ({ups} up, {downs} down)"}
    detail = f"{ups} up vs {downs} down estimate revisions in 30 days (tracking only, not used in decisions yet)"
    if downs >= 2 * max(ups, 1) and downs > ups:
        return {"status": "WARN", "detail": detail}
    if ups >= 2 * max(downs, 1) and ups > downs:
        return {"status": "POSITIVE", "detail": detail}
    return {"status": "NEUTRAL", "detail": detail}


def run(ticker: str, metrics: pd.DataFrame, as_of: date | str | None = None) -> dict[str, dict]:
    as_of = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    out = {"insider_activity": _check(lambda: insiders(ticker)),
           "corporate_events": _check(lambda: events(ticker, bool(metrics.loc[ticker].get("is_financial")))),
           "price_behaviour": _check(lambda: price_tells(ticker, metrics, as_of)),
           "analyst_revisions": _check(lambda: revisions(ticker)),
           "upcoming_results": _check(lambda: upcoming_results(ticker, as_of))}
    out["revision_breadth"] = _check(lambda: revision_breadth(ticker))  # after revisions(): reuses its cache
    out["forward_earnings"] = _check(lambda: forward_earnings(ticker, metrics))
    return out
