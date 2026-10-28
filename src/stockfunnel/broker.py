"""Paper broker with pre-trade risk checks and kill switches.

Autopilot only ever trades here. A live adapter (Alpaca for US, Kite Connect for India) should
implement the same contract plus a limit-price band vs the live quote, and only be wired in after
>= 6 months of clean paper trading (docs/research/llm-guardrails.md, promotion ladder).
Paper fills use the signal-day close + modeled costs, which is mildly optimistic.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date, timedelta
from pathlib import Path

from . import data, journal
from .policy import PORTFOLIO, RISK

STATE_DIR = Path("state")


def load(market: str, capital: float | None = None) -> dict:
    path = STATE_DIR / f"{market}.json"
    if path.exists():
        return json.loads(path.read_text())
    cap = capital or (100_000.0 if market == "US" else 10_000_000.0)
    return {"market": market, "universe": None, "cash": cap, "positions": {}, "peak_nav": cap, "halted": False,
            "nav_history": {}, "watchlist": {}, "vetoes": {}, "executed": [], "trades": [], "last_rebalance": None}


def state_hash(state: dict) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


def save(state: dict) -> None:
    """Persist and anchor the state's hash in the journal, so out-of-band edits are detectable."""
    STATE_DIR.mkdir(exist_ok=True)
    path = STATE_DIR / f"{state['market']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    journal.record("state", {"market": state["market"], "sha": state_hash(state)})
    os.replace(tmp, path)  # atomic: a crash leaves either the old or the new file, never a torn one


def tampered(state: dict) -> bool:
    """True if the state differs from the last version this code saved, or the file was deleted
    after having existed (deleting state must not reset halts, peaks and vetoes).
    ponytail: the anchor is unkeyed, so an agent with shell access could forge a matching journal
    entry; MCP-only agents can't. Upgrade path: HMAC the state hash with a key outside the repo."""
    recs = [e for e in journal.entries("state") if e["payload"]["market"] == state["market"]]
    if not (STATE_DIR / f"{state['market']}.json").exists():
        return bool(recs)
    return not recs or recs[-1]["payload"]["sha"] != state_hash(state)


def nav(state: dict, last: dict[str, float]) -> float | None:
    """None if any held position has no price: unknown value must never read as 'no loss'."""
    if any(t not in last for t in state["positions"]):
        return None
    return state["cash"] + sum(p["shares"] * last[t] for t, p in state["positions"].items())


def priced_nav(state: dict, last: dict[str, float]) -> float:
    """Cash + positions that have a price (unpriced ones excluded, not valued at cost)."""
    return state["cash"] + sum(p["shares"] * last[t] for t, p in state["positions"].items() if t in last)


def weights(state: dict, last: dict[str, float]) -> dict[str, float]:
    """Weights of priced positions; unpriced holdings appear with weight 0 so they're never forgotten."""
    total = priced_nav(state, last)
    return {t: (p["shares"] * last[t] / total if t in last else 0.0) for t, p in state["positions"].items()}


def risk_status(state: dict, last: dict[str, float], as_of: date) -> dict:
    """Compute once per run. Daily loss compares to the NAV recorded on the previous *trading date*,
    so repeated runs on the same day can't reset it. A halt is cleared only by `stockfunnel reset-halt`."""
    n = nav(state, last)
    if n is None:
        missing = sorted(t for t in state["positions"] if t not in last)
        return {"nav": None, "priced_nav": round(priced_nav(state, last), 2), "drawdown": None, "day_return": None,
                "halted": state["halted"], "buys_paused": True,
                "reason": f"no recent price for held positions {missing} (human: `stockfunnel write-off`)"}
    hist = state.setdefault("nav_history", {})
    prior = [d for d in hist if d < as_of.isoformat()]
    day_ret = n / hist[max(prior)] - 1 if prior else 0.0
    hist[as_of.isoformat()] = n
    state["peak_nav"] = max(state["peak_nav"], n)
    dd = n / state["peak_nav"] - 1
    if dd <= RISK["halt_autopilot_drawdown"]:
        state["halted"] = True
    paused = dd <= RISK["pause_buys_drawdown"] or day_ret <= RISK["max_daily_loss"] or state["halted"]
    return {"nav": round(n, 2), "drawdown": round(dd, 4), "day_return": round(day_ret, 4),
            "halted": state["halted"], "buys_paused": paused}


def decision_id(market: str, ticker: str, action: str, target: float, as_of: date) -> str:
    return hashlib.sha256(f"{market}|{ticker}|{action}|{target:.4f}|{as_of}".encode()).hexdigest()[:16]


def _turnover_1y(state: dict, as_of: date, total: float) -> float:
    since = (as_of - timedelta(days=365)).isoformat()
    return sum(abs(t["shares"] * t["price"]) for t in state["trades"] if t["date"] >= since and t["shares"] > 0) / total


def execute(state: dict, actions: dict[str, dict], last: dict[str, float], as_of: date, status: dict,
            adv: dict[str, float], today: date | None = None) -> list[dict]:
    """Executes final actions against the paper book. Every order passes the checks or is refused.
    Risk-reducing orders (SELL/TRIM) are never blocked by budgets or pauses, only by missing data."""
    today = today or date.today()
    results = []
    total = status["nav"] or status.get("priced_nav")  # sells still work if one holding is unpriced
    cost = data.MARKETS[state["market"]]["cost_bps"] / 2 / 1e4   # one-way
    month = as_of.strftime("%Y-%m")
    buys_this_month = sum(1 for t in state["trades"] if t["date"].startswith(month) and t["shares"] > 0)

    order = sorted(actions.items(), key=lambda kv: kv[1]["action"] not in ("SELL", "TRIM"))  # sells first
    for t, a in order:
        act = a["action"]
        if act not in ("BUY", "SELL", "TRIM"):
            continue
        did = decision_id(state["market"], t, act, a.get("target_weight", 0.0), as_of)
        price, refuse = last.get(t), None
        if did in state["executed"]:
            refuse = "duplicate decision (idempotency)"
        elif not total:
            refuse = "NAV unknown"
        elif price is None or price <= 0:
            refuse = "no valid last price"
        elif (today - as_of).days > RISK["max_data_age_days"]:
            refuse = f"stale data ({as_of})"
        elif act == "BUY":
            if status["nav"] is None:
                refuse = status.get("reason", "NAV unknown")
            elif state["halted"]:
                refuse = "autopilot halted; human reset required"
            elif status["buys_paused"]:
                refuse = f"buys paused by kill switch (drawdown {status['drawdown']:.1%}, day {status['day_return']:.1%})"
            elif buys_this_month >= PORTFOLIO["max_trades_per_month"]:
                refuse = "monthly buy budget exhausted"
            elif _turnover_1y(state, as_of, total) >= PORTFOLIO["max_annual_turnover"]:
                refuse = "annual turnover budget exhausted"
        if refuse:
            results.append({"ticker": t, "action": act, "status": "REFUSED", "reason": refuse})
            continue

        pos = state["positions"].get(t, {"shares": 0, "avg_cost": price})
        delta_value = a.get("target_weight", 0.0) * total - pos["shares"] * price
        if act == "SELL":
            delta_value = -pos["shares"] * price
        if act == "BUY":
            delta_value = min(delta_value, RISK["max_order_fraction_nav"] * total,
                              PORTFOLIO["max_adv_fraction"] * adv.get(t, 0.0), state["cash"] / (1 + cost))
        if act in ("SELL", "TRIM"):
            delta_value = min(delta_value, 0.0)  # a 'sell' can never buy
        fill = price * (1 + cost) if delta_value > 0 else price * (1 - cost)
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
                                     "opened": pos.get("opened", as_of.isoformat())}
        trade = {"date": as_of.isoformat(), "ticker": t, "action": act, "shares": shares,
                 "price": round(fill, 4), "decision_id": did, "reason": a.get("reason")}
        state["trades"].append(trade)
        state["executed"].append(did)
        buys_this_month += shares > 0
        journal.record("trade", {"market": state["market"], **trade})
        results.append({**trade, "status": "FILLED"})
    return results
