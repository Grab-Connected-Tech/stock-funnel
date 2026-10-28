"""Paper broker with pre-trade risk checks and kill switches.

Autopilot only ever trades here. A live adapter (Alpaca for US, Kite Connect for India) should
implement the same `submit` contract and only be wired in after >= 6 months of clean paper
trading (docs/research/llm-guardrails.md, promotion ladder).
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

from . import data, journal
from .policy import PORTFOLIO, RISK

STATE_DIR = Path("state")


def load(market: str, capital: float | None = None) -> dict:
    path = STATE_DIR / f"{market}.json"
    if path.exists():
        return json.loads(path.read_text())
    cap = capital or (100_000.0 if market == "US" else 10_000_000.0)
    return {"market": market, "cash": cap, "positions": {}, "peak_nav": cap, "halted": False,
            "watchlist": {}, "executed": [], "trades": []}


def save(state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    (STATE_DIR / f"{state['market']}.json").write_text(json.dumps(state, indent=2, default=str))


def nav(state: dict, last: dict[str, float]) -> float:
    return state["cash"] + sum(p["shares"] * last.get(t, p["avg_cost"]) for t, p in state["positions"].items())


def weights(state: dict, last: dict[str, float]) -> dict[str, float]:
    total = nav(state, last)
    return {t: p["shares"] * last.get(t, p["avg_cost"]) / total for t, p in state["positions"].items()}


def risk_status(state: dict, last: dict[str, float]) -> dict:
    """Updates peak NAV and the halt flag. A halt can only be cleared by a human (`stockfunnel reset-halt`)."""
    n = nav(state, last)
    state["peak_nav"] = max(state["peak_nav"], n)
    dd = n / state["peak_nav"] - 1
    day_ret = n / state.get("last_nav", n) - 1
    state["last_nav"] = n
    if dd <= RISK["halt_autopilot_drawdown"]:
        state["halted"] = True
    return {"nav": round(n, 2), "drawdown": round(dd, 4), "day_return": round(day_ret, 4), "halted": state["halted"],
            "buys_paused": dd <= RISK["pause_buys_drawdown"] or day_ret <= RISK["max_daily_loss"] or state["halted"]}


def decision_id(market: str, ticker: str, action: str, target: float, day: date) -> str:
    return hashlib.sha256(f"{market}|{ticker}|{action}|{target:.4f}|{day}".encode()).hexdigest()[:16]


def execute(state: dict, actions: dict[str, dict], last: dict[str, float], last_date: date, today: date | None = None) -> list[dict]:
    """Executes final actions against the paper book. Every order passes the checks or is refused."""
    today = today or date.today()
    results = []
    status = risk_status(state, last)
    total = status["nav"]
    cost = data.MARKETS[state["market"]]["cost_bps"] / 2 / 1e4   # one-way
    month = today.strftime("%Y-%m")
    trades_this_month = sum(1 for t in state["trades"] if t["date"].startswith(month))

    # Sells first so cash is available for buys.
    order = sorted(actions.items(), key=lambda kv: kv[1]["action"] != "SELL" and kv[1]["action"] != "TRIM")
    for t, a in order:
        act = a["action"]
        if act not in ("BUY", "SELL", "TRIM"):
            continue
        did = decision_id(state["market"], t, act, a.get("target_weight", 0.0), today)
        refuse = None
        price = last.get(t)
        if did in state["executed"]:
            refuse = "duplicate decision (idempotency)"
        elif price is None or price <= 0:
            refuse = "no valid last price"
        elif (today - last_date).days > RISK["max_data_age_days"]:
            refuse = f"stale data ({last_date})"
        elif trades_this_month >= PORTFOLIO["max_trades_per_month"]:
            refuse = "monthly trade budget exhausted"
        elif act == "BUY" and status["buys_paused"]:
            refuse = f"buys paused by drawdown kill switch ({status['drawdown']:.1%})"
        elif state["halted"]:
            refuse = "autopilot halted; human reset required"
        if refuse:
            results.append({"ticker": t, "action": act, "status": "REFUSED", "reason": refuse})
            continue

        pos = state["positions"].get(t, {"shares": 0, "avg_cost": price})
        target_value = a.get("target_weight", 0.0) * total
        delta_value = target_value - pos["shares"] * price
        if act == "SELL":
            delta_value = -pos["shares"] * price
        if act == "BUY":
            delta_value = min(delta_value, RISK["max_order_fraction_nav"] * total, state["cash"] / (1 + cost))
        fill = price * (1 + cost) if delta_value > 0 else price * (1 - cost)
        if abs(fill / price - 1) > RISK["limit_price_band"]:
            results.append({"ticker": t, "action": act, "status": "REFUSED", "reason": "fill outside limit band"})
            continue
        shares = int(delta_value / fill)  # whole shares, rounds toward zero
        if shares == 0:
            results.append({"ticker": t, "action": act, "status": "SKIPPED", "reason": "order rounds to 0 shares"})
            continue

        state["cash"] -= shares * fill
        new_shares = pos["shares"] + shares
        if new_shares <= 0:
            state["positions"].pop(t, None)
        else:
            avg = pos["avg_cost"] if shares < 0 else (pos["shares"] * pos["avg_cost"] + shares * fill) / new_shares
            state["positions"][t] = {"shares": new_shares, "avg_cost": round(avg, 4),
                                     "opened": pos.get("opened", today.isoformat())}
        trade = {"date": today.isoformat(), "ticker": t, "action": act, "shares": shares,
                 "price": round(fill, 4), "decision_id": did, "reason": a.get("reason")}
        state["trades"].append(trade)
        state["executed"].append(did)
        trades_this_month += 1
        journal.record("trade", {"market": state["market"], **trade})
        results.append({**trade, "status": "FILLED"})
    return results
