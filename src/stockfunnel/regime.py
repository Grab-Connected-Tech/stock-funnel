"""Market regime: how much equity exposure is allowed, and whether momentum is in a crash-prone state.
Only the two rules that survive out-of-sample (see docs/research/factors.md). No forecasting."""
from __future__ import annotations

import numpy as np

from . import data

PANIC_VOL_INDEX = {"US": 30.0, "IN": 25.0}


def assess(market: str) -> dict:
    m = data.MARKETS[market]
    px = data.prices([m["benchmark"], m["vol_index"]], period="5y")["close"]
    idx, vix = px[m["benchmark"]].dropna(), px[m["vol_index"]].dropna()

    # Rule 1 (Faber 2007): month-end close vs 10-month SMA. Below -> cap equity at 50%.
    monthly = idx.resample("ME").last()
    monthly = monthly[monthly.index.tz_localize(None) <= idx.index[-1].tz_localize(None)]  # completed months only
    trend_up = bool(monthly.iloc[-1] > monthly.iloc[-10:].mean())

    # Rule 2 (Daniel & Moskowitz 2016 panic state): bear market + high vol -> momentum crash risk.
    rets = idx.pct_change().dropna()
    vol_1m = rets.rolling(21).std() * np.sqrt(252)
    vol_pct = float((vol_1m[-756:] < vol_1m.iloc[-1]).mean())
    ret_24m = float(idx.iloc[-1] / idx.iloc[-504] - 1) if len(idx) > 504 else 0.0
    panic = ret_24m < 0 and (vol_pct > 0.8 or float(vix.iloc[-1]) > PANIC_VOL_INDEX[market])

    exposure = 1.0 if trend_up else 0.5
    return {
        "market": market,
        "label": "risk_on" if trend_up and not panic else "risk_off" if not trend_up else "panic",
        "max_equity_exposure": exposure,
        "momentum_crash_guard": panic,
        "index_month_end_above_10m_sma": trend_up,
        "index_return_24m": round(ret_24m, 3),
        "realized_vol_1m_percentile_3y": round(vol_pct, 2),
        "vol_index": round(float(vix.iloc[-1]), 2),
        "index_drawdown_from_1y_high": round(float(idx.iloc[-1] / idx[-252:].max() - 1), 3),
    }
