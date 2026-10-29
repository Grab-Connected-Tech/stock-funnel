"""Automated pre-trade checks from free official sources (NSE, SEC EDGAR). No keys, no LLMs.

Each check returns {"status": PASS | WARN | FAIL | UNKNOWN, "detail": str}.
  FAIL    -> the stock is avoided (or sold if held)
  WARN    -> listed as a risk, costs a conviction point
  UNKNOWN -> source unreachable; a CRITICAL check that is unknown blocks a buy (fail-closed)
Bulk lists are cached for a day, per-company lookups for a week.
"""
from __future__ import annotations

import io
import json
import os
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

import pandas as pd

from .data import _cached

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
      "Accept": "application/json,text/csv,*/*", "Referer": "https://www.nseindia.com/"}
# SEC asks automated clients to identify themselves; set STOCKFUNNEL_SEC_UA="name email" to add a contact.
SEC_UA = {"User-Agent": os.environ.get("STOCKFUNNEL_SEC_UA", "stockfunnel/0.1 personal research tool")}
CRITICAL = {"surveillance", "price_band", "restatement"}
PLEDGE_MAX, PLEDGE_JUMP = 5.0, 5.0      # % of promoter holding encumbered; quarter-on-quarter rise in pp
LOOKBACK_DAYS = 730


def _get(url: str, headers: dict, timeout: int = 30) -> bytes:
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout).read()


def _nse_json(path: str):
    return json.loads(_get("https://www.nseindia.com/api/" + path, UA))


def _check(fn):
    """Run a check; any network/format failure becomes UNKNOWN rather than a crash or a silent pass."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return {"status": "UNKNOWN", "detail": f"source unavailable ({type(e).__name__})"}


# ---------------- India ----------------

def _surveillance_lists() -> dict[str, str]:
    def fetch():
        out = {}
        asm = _nse_json("reportASM")
        for term in ("longterm", "shortterm"):
            for row in asm.get(term, {}).get("data", []):
                out[row["symbol"]] = row.get("survDesc") or row.get("asmSurvIndicator") or "ASM"
        for name in ("reportGSM", "reportESM"):
            got = _nse_json(name)
            rows = got if isinstance(got, list) else got.get("data", [])
            for row in rows:
                out[row["symbol"]] = row.get("survDesc") or row.get("gsmStage") or row.get("esmSurvIndicator") or name[6:]
        return out
    return _cached("nse_surveillance", fetch, max_age_days=1)


def _price_bands() -> dict[str, str]:
    def fetch():
        df = pd.read_csv(io.BytesIO(_get("https://nsearchives.nseindia.com/content/equities/sec_list.csv", UA)))
        df.columns = [c.strip() for c in df.columns]
        eq = df[df["Series"].str.strip().isin(["EQ", "BE", "BZ"])]
        return {s.strip(): str(b).strip() for s, b in zip(eq["Symbol"], eq["Band"])}
    return _cached("nse_price_bands", fetch, max_age_days=1)


def india(symbol: str) -> dict[str, dict]:
    """symbol without .NS"""
    def surveillance():
        hit = _surveillance_lists().get(symbol)
        return ({"status": "FAIL", "detail": f"on NSE surveillance list: {hit}"} if hit
                else {"status": "PASS", "detail": "not on ASM/GSM/ESM lists"})

    def price_band():
        band = _price_bands().get(symbol)
        if band is None:
            return {"status": "UNKNOWN", "detail": "not found in NSE price-band file"}
        if band.lower() == "no band":
            return {"status": "PASS", "detail": "F&O stock: no fixed circuit limit"}
        return ({"status": "PASS", "detail": f"{band}% price band"} if float(band) >= 20
                else {"status": "FAIL", "detail": f"tight {band}% price band: risk of being locked in a lower circuit"})

    def pledge():
        rows = _cached(f"nse_pledge_{symbol}", lambda: _nse_json(
            "corporate-pledgedata?index=equities&symbol=" + urllib.parse.quote(symbol)).get("data", []), max_age_days=7)
        recs = []
        for r in rows:
            try:
                recs.append((datetime.strptime(r["shp"].strip(), "%d-%b-%Y"), float(str(r["percPromoterShares"]).strip() or 0)))
            except (KeyError, ValueError):
                continue
        if not recs:
            return {"status": "PASS", "detail": "no promoter pledge disclosures on record"}
        recs.sort()
        d, cur = recs[-1]
        prev = recs[-2][1] if len(recs) > 1 else cur
        if cur > PLEDGE_MAX:
            return {"status": "FAIL", "detail": f"{cur:.1f}% of promoter holding pledged ({d:%b %Y})"}
        if cur - prev > PLEDGE_JUMP:
            return {"status": "FAIL", "detail": f"promoter pledge jumped {prev:.1f}% → {cur:.1f}%"}
        return {"status": "PASS", "detail": f"{cur:.1f}% of promoter holding pledged ({d:%b %Y})"}

    def auditor():
        frm, to = (date.today() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%m-%Y"), date.today().strftime("%d-%m-%Y")
        found = []
        for subject in ("Resignation of Statutory Auditor", "Initiation of Forensic Audit"):
            q = urllib.parse.urlencode({"index": "equities", "symbol": symbol, "from_date": frm, "to_date": to, "subject": subject})
            rows = _cached(f"nse_ann_{symbol}_{subject[:12]}", lambda q=q: _nse_json("corporate-announcements?" + q), max_age_days=7)
            found += [f"{subject.lower()} ({str(r.get('an_dt', ''))[:11]})" for r in rows or []]
        return ({"status": "FAIL", "detail": "; ".join(found[:2])} if found
                else {"status": "PASS", "detail": "no auditor resignation or forensic audit in 2 years"})

    return {"surveillance": _check(surveillance), "price_band": _check(price_band),
            "promoter_pledge": _check(pledge), "auditor_events": _check(auditor)}


# ---------------- US ----------------

def _cik(ticker: str) -> int | None:
    """S&P 500 constituents file carries CIKs (no SEC call needed); other tickers use SEC's map, which
    www.sec.gov only serves to clients that identify themselves (set STOCKFUNNEL_SEC_UA="name email")."""
    from .data import UNIVERSES
    sp = _cached("sp500_cik", lambda: {str(r.Symbol).replace(".", "-"): int(r.CIK) for r in pd.read_csv(
        io.BytesIO(_get(UNIVERSES["sp500"][1], UA))).itertuples()}, max_age_days=7)
    if ticker in sp:
        return sp[ticker]
    m = _cached("sec_tickers", lambda: {v["ticker"]: v["cik_str"] for v in json.loads(
        _get("https://www.sec.gov/files/company_tickers.json", SEC_UA)).values()}, max_age_days=7)
    return m.get(ticker.replace("-", "."), m.get(ticker))


def us(ticker: str) -> dict[str, dict]:
    cik = _check(lambda: {"cik": _cik(ticker)})
    if cik.get("status") == "UNKNOWN" or not cik.get("cik"):
        unk = {"status": "UNKNOWN", "detail": "company not found in SEC EDGAR"}
        return {"restatement": unk, "auditor_change": unk, "going_concern": unk}
    c10 = f"{cik['cik']:010d}"
    since = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()

    def eight_ks():
        sub = _cached(f"sec_sub_{c10}", lambda: json.loads(_get(f"https://data.sec.gov/submissions/CIK{c10}.json", SEC_UA)),
                      max_age_days=7)
        r = sub["filings"]["recent"]
        return [(d, i) for f, d, i in zip(r["form"], r["filingDate"], r["items"]) if f.startswith("8-K") and d >= since]

    def restatement():
        hits = [d for d, i in eight_ks() if "4.02" in i]
        return ({"status": "FAIL", "detail": f"financial restatement (8-K Item 4.02) filed {hits[0]}"} if hits
                else {"status": "PASS", "detail": "no restatements in 2 years"})

    def auditor_change():
        hits = [d for d, i in eight_ks() if "4.01" in i]
        return ({"status": "WARN", "detail": f"auditor changed (8-K Item 4.01) on {hits[0]}"} if hits
                else {"status": "PASS", "detail": "no auditor change in 2 years"})

    def going_concern():
        q = urllib.parse.urlencode({"q": '"substantial doubt about" "ability to continue as a going concern"',
                                    "forms": "10-K,10-Q", "ciks": c10, "dateRange": "custom",
                                    "startdt": since, "enddt": date.today().isoformat()})
        got = _cached(f"sec_gc_{c10}", lambda: json.loads(_get("https://efts.sec.gov/LATEST/search-index?" + q, SEC_UA)),
                      max_age_days=7)
        n = got.get("hits", {}).get("total", {}).get("value", 0)
        # ponytail: phrase search also matches boilerplate risk factors, so this warns rather than fails.
        return ({"status": "WARN", "detail": f"'going concern' doubt language in {n} recent 10-K/10-Q filing(s)"} if n
                else {"status": "PASS", "detail": "no going-concern doubt language in 2 years"})

    return {"restatement": _check(restatement), "auditor_change": _check(auditor_change),
            "going_concern": _check(going_concern)}


def run(ticker: str) -> dict[str, dict]:
    return india(ticker.rsplit(".", 1)[0]) if ticker.endswith((".NS", ".BO")) else us(ticker)


NOT_COVERED = {
    "IN": "Qualified audit opinions and related-party transactions live only in PDF annual reports; not checked.",
    "US": "SEC enforcement actions aren't indexed by ticker; not checked.",
}
