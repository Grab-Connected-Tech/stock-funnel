"""Data layer. Free default: yfinance (US + NSE/BSE). Everything is cached on disk per day
so a run is reproducible and the AI can't "re-roll" numbers mid-decision.

Swap providers by replacing these functions; the rest of the framework only sees
plain pandas objects with yfinance line-item names.
"""
from __future__ import annotations

import hashlib
import io
import pickle
import urllib.request
import warnings
from datetime import date
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore", message=".*inplace keyword.*")  # yfinance vs pandas 3 noise
import yfinance as yf  # noqa: E402

CACHE = Path.home() / ".cache" / "stockfunnel"

MARKETS = {
    # benchmark index, volatility index, currency, round-trip cost in bps (brokerage+taxes+slippage)
    "US": {"benchmark": "^GSPC", "vol_index": "^VIX", "ccy": "USD", "cost_bps": 10},
    # India: STT 0.1% each side on delivery + stamp + exchange + slippage
    "IN": {"benchmark": "^CRSLDX", "vol_index": "^INDIAVIX", "ccy": "INR", "cost_bps": 35},
}

UNIVERSES = {
    "sp500": ("US", "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv", "Symbol", ""),
    "nifty500": ("IN", "https://archives.nseindia.com/content/indices/ind_nifty500list.csv", "Symbol", ".NS"),
    "nifty200": ("IN", "https://archives.nseindia.com/content/indices/ind_nifty200list.csv", "Symbol", ".NS"),
    "nifty50": ("IN", "https://archives.nseindia.com/content/indices/ind_nifty50list.csv", "Symbol", ".NS"),
}


def market_of(ticker: str) -> str:
    return "IN" if ticker.endswith((".NS", ".BO")) else "US"


def _cached(key: str, fn, max_age_days: int = 1):
    """Pickle cache keyed by name; stale after max_age_days."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{key.replace('/', '_').replace('^', '_')}.pkl"
    if path.exists() and (date.today() - date.fromtimestamp(path.stat().st_mtime)).days < max_age_days:
        return pickle.loads(path.read_bytes())
    value = fn()
    path.write_bytes(pickle.dumps(value))
    return value


def universe(name: str) -> tuple[str, list[str]]:
    """Returns (market, tickers). `name` is a key of UNIVERSES or a path to a file with one ticker per line."""
    if Path(name).exists():
        tickers = [t.strip() for t in Path(name).read_text().split() if t.strip()]
        return market_of(tickers[0]), tickers
    market, url, col, suffix = UNIVERSES[name]

    def fetch():
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})  # NSE rejects default UA
        df = pd.read_csv(io.BytesIO(urllib.request.urlopen(req, timeout=30).read()))
        return [f"{s}{suffix}" if suffix else str(s).replace(".", "-") for s in df[col]]  # BRK.B -> BRK-B

    return market, _cached(f"universe_{name}", fetch, max_age_days=7)


def prices(tickers: list[str], period: str = "3y") -> pd.DataFrame:
    """Adjusted daily closes, one column per ticker."""
    def fetch():
        df = yf.download(tickers, period=period, auto_adjust=True, progress=False, threads=True)
        close, vol = df["Close"], df["Volume"]
        if isinstance(close, pd.Series):
            close, vol = close.to_frame(tickers[0]), vol.to_frame(tickers[0])
        return {"close": close, "volume": vol}

    key = f"prices_{period}_" + hashlib.sha1(",".join(sorted(tickers)).encode()).hexdigest()[:12]
    return _cached(key, fetch)


def fundamentals(ticker: str) -> dict:
    """Annual statements (columns newest-first) + info dict. Cached a week (statements change quarterly)."""
    def fetch():
        t = yf.Ticker(ticker)
        try:
            info = t.info or {}
        except Exception:
            info = {}
        return {
            "income": t.income_stmt,
            "balance": t.balance_sheet,
            "cashflow": t.cashflow,
            "info": info,
        }

    return _cached(f"fund_{ticker}", fetch, max_age_days=7)
