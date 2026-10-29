"""Stage 9: one clear, self-contained recommendation per stock. Pure rules over data, no judgment calls.

Actions:  BUY · BUY AFTER <date> · WAIT · HOLD · SELL · AVOID
Conviction is a transparent points tally (never a forecast), capped at MEDIUM where the strategy has
no proven edge in that market.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from .checks import CRITICAL
from .policy import GATES, PORTFOLIO
from .portfolio import capped_weights, tax_deferral_days

# gate -> (metric, limit, "min"|"max", label, comfort): `comfort` is the distance from the limit we
# consider safely clear, in the metric's own units (a fixed judgment per gate, not fitted).
GATE_LIMITS = {
    "piotroski": ("piotroski", GATES["piotroski_min"], "min", "Piotroski F-score", 3),
    "altman_z2": ("altman_z2", GATES["altman_z2_min"], "min", "Altman Z'' (bankruptcy risk)", 1.5),
    "beneish_m": ("beneish_m", GATES["beneish_m_max"], "max", "Beneish M (manipulation risk)", 0.8),
    "montier_c": ("montier_c", GATES["montier_c_max"], "max", "Montier C (accounting red flags)", 2),
    "accruals": ("accruals", GATES["accruals_max"], "max", "Accruals (profit not backed by cash)", 0.10),
    "leverage": ("debt_to_equity", GATES["max_debt_to_equity"], "max", "Debt / equity", 1.0),
    "interest_coverage": ("interest_coverage", GATES["min_interest_coverage"], "min", "Interest coverage", 5),
    "dilution": ("net_issuance_1y", GATES["max_net_issuance_1y"], "max", "New shares issued (1y)", 0.07),
    "not_lottery_stock": ("vol_percentile", GATES["max_vol_percentile"], "max", "Volatility percentile", 0.2),
}
NEAR = 0.25  # within a quarter of the comfort distance = "close to the rejection line"
EVIDENCE_SCOPE = ("Backtested: the price factors (momentum, low volatility, trend). NOT backtested: value, quality, "
                  "yield, the safety gates, checks and hidden signals; they are evidence-based rules from published "
                  "research, but unproven in this system. Conviction is a rule-of-thumb tally, not a probability.")
ETF_ALTERNATIVE = {
    "US": "Over 2011-2026 a low-cost factor ETF (USMV or QUAL) matched this strategy's risk-adjusted return "
          "with no stock picking. In the US, owning that ETF is an equally good default.",
    "IN": "No proven edge on record for India right now; a Nifty 50 index fund is the default alternative.",
}
GATE_NAMES = {
    "history_1y": "under 1 year of history", "min_price": "penny stock", "liquidity": "too thinly traded",
    "data_quality": "suspicious price data", "no_stale_trading": "irregular trading", "min_market_cap": "too small",
    "not_lottery_stock": "extremely volatile", "positive_equity": "negative equity", "dilution": "issuing many new shares",
    "piotroski": "weakening fundamentals (Piotroski)", "accruals": "profits not backed by cash",
    "cash_generation": "burning operating cash", "altman_z2": "bankruptcy risk (Altman)",
    "beneish_m": "earnings-manipulation pattern (Beneish)", "montier_c": "accounting red flags (Montier)",
    "leverage": "too much debt", "interest_coverage": "profits barely cover interest",
    "financial_roa": "weak return on assets", "financial_roe": "weak return on equity",
}
THEME_NAMES = {"value": "valuation", "quality": "business quality", "momentum": "price trend",
               "low_vol": "price stability", "yield": "cash returned to shareholders"}


def _margin(v: float, limit: float, kind: str, comfort: float) -> float:
    """How far a value is inside its limit, in units of the gate's comfort distance (0 = on the line)."""
    return ((v - limit) if kind == "min" else (limit - v)) / comfort


def nearest_gates(metrics: dict, checks: dict, k: int = 3) -> list[dict]:
    out = []
    for gate, (metric, limit, kind, label, comfort) in GATE_LIMITS.items():
        v = metrics.get(metric)
        if checks.get(gate) == "PASS" and isinstance(v, (int, float)) and v == v:
            out.append({"gate": gate, "label": label, "value": round(float(v), 3), "limit": limit,
                        "rule": f"{'falls below' if kind == 'min' else 'rises above'} {limit}",
                        "margin": round(_margin(float(v), limit, kind, comfort), 3)})
    return sorted(out, key=lambda x: x["margin"])[:k]


def target_weight(ticker: str, scored: pd.DataFrame, market: str, exposure: float) -> float | None:
    """The weight the funnel would give this stock inside its top-N book (inverse vol, caps, regime)."""
    n = PORTFOLIO["n_holdings"][market]
    book = scored[scored["rank"] <= n]
    if ticker not in book.index:
        book = pd.concat([book.iloc[: n - 1], scored.loc[[ticker]]])
    w = capped_weights(1 / book["vol_1y"], book["sector"], PORTFOLIO["max_weight"][market], PORTFOLIO["max_sector_weight"])
    w = w[w >= PORTFOLIO["min_weight"]] * exposure
    return round(float(w.get(ticker, 0.0)), 4) or None


def build(r: dict, fm: dict, scored: pd.DataFrame, validation: dict, checks: dict | None = None,
          today: date | None = None, signals: dict | None = None) -> dict:
    """r: the analyze_stock report; fm: the stock's full metric row.
    checks: automated pre-trade checks {name: {"status": PASS|FAIL|UNKNOWN, "detail": str}}."""
    today = date.fromisoformat(r["as_of"]) if today is None else today
    market = "IN" if r["ticker"].endswith((".NS", ".BO")) else "US"
    m, g, reg, book = r["metrics"], r["gates"], r["regime"], r["your_book"]
    held, rank, n, buffer = book["held"], r["rank"], PORTFOLIO["n_holdings"][market], PORTFOLIO["hold_until_rank"][market]
    checks = checks or {}
    failed_checks = [f"{k.replace('_', ' ')}: {v['detail']}" for k, v in checks.items() if v["status"] == "FAIL"]
    warn_checks = [f"{k.replace('_', ' ')}: {v['detail']}" for k, v in checks.items() if v["status"] == "WARN"]
    unverified = [k.replace("_", " ") for k, v in checks.items() if v["status"] == "UNKNOWN" and k in CRITICAL]
    signals = signals or {}
    failed_checks += [f"{k.replace('_', ' ')}: {v['detail']}" for k, v in signals.items() if v["status"] == "FAIL"]
    warn_checks += [f"{k.replace('_', ' ')}: {v['detail']}" for k, v in signals.items() if v["status"] == "WARN"]
    waits = [v["detail"] for v in signals.values() if v["status"] == "WAIT"]
    positives = [v["detail"] for v in signals.values() if v["status"] == "POSITIVE"]
    why, risks = [], []

    themes = sorted(r["themes"].items(), key=lambda kv: -kv[1])
    for t, z in themes[:2]:
        if z > 0.3:
            why.append(f"Better {THEME_NAMES[t]} than {_pct(z)}% of its sector peers")
    for t, z in themes[::-1][:2]:
        if z < -0.3:
            risks.append(f"Worse {THEME_NAMES[t]} than {100 - _pct(z)}% of its sector peers")
    near = nearest_gates(fm, g["checks"])
    for x in near:
        if x["margin"] < NEAR:
            risks.append(f"{x['label']} is close to the rejection line ({x['value']} vs limit {x['limit']})")
    risks = warn_checks + risks
    if reg["max_equity_exposure"] < 1:
        risks.append(f"Market is risk-off: the system keeps only {reg['max_equity_exposure']:.0%} of money in stocks")
    if (m.get("max_dd_1y") or 0) < -0.3:
        risks.append(f"Fell {abs(m['max_dd_1y']):.0%} from its peak within the past year")

    # ---- action ----
    start = None
    if g["failed"] or not g["passed"]:
        action = "SELL" if held else "AVOID"
        why = ["Fails safety gate(s): " + ", ".join(GATE_NAMES.get(x, x) for x in (g["failed"] or g["unknown"]))]
        why += ["Fails pre-trade check: " + c for c in failed_checks]
    elif failed_checks:
        action = "SELL" if held else "AVOID"
        why = ["Fails pre-trade check(s): " + "; ".join(failed_checks)]
    elif book.get("standing_veto"):
        action, why = ("HOLD" if held else "AVOID"), [f"Standing veto: {book['standing_veto']['reason']}"]
    elif rank is None:
        action, why = ("SELL" if held else "AVOID"), ["Too little data to rank"]
    elif rank > buffer:
        wait = tax_deferral_days(market, book.get("opened"), book.get("gain"), today) if held else 0
        action = "HOLD" if wait else "SELL" if held else "AVOID"
        why.insert(0, f"Ranks {rank} of {r['ranked_peers']}: outside the top {buffer}; better options exist")
        if wait:
            start = today + timedelta(days=wait)
            why.insert(1, f"Sell on {start} ({wait} days): that qualifies the gain for 12.5% long-term tax instead of 20%")
    elif rank > n:
        action = "HOLD" if held else "WAIT"
        why.insert(0, f"Ranks {rank}: good enough to keep, not strong enough to start a new position (top {n})")
    elif (m_ret := fm.get("ret_1m")) is not None and m_ret > PORTFOLIO["max_1m_return_for_buy"]:
        action = "WAIT"
        why.insert(0, f"Ranks {rank}, but jumped {m_ret:.0%} this month; the system doesn't chase spikes")
    elif waits and not held and rank <= n:
        action = "WAIT"
        why.insert(0, f"Ranks {rank}, but something unexplained just happened: {waits[0]}. Wait for it to settle")
    elif unverified and not held:
        action = "WAIT"
        why.insert(0, f"Ranks {rank}, but couldn't verify {', '.join(unverified)} today (source unreachable); retry later")
    else:
        since = date.fromisoformat(book["watchlist_since"]) if book.get("watchlist_since") else today
        start = since + timedelta(days=PORTFOLIO["cooling_off_days"])
        action = "HOLD" if held else ("BUY" if start <= today else "BUY AFTER")
        why.insert(0, f"Ranks {rank} of {r['ranked_peers']} after passing every safety gate")

    # ---- conviction: transparent points, not a forecast ----
    pts, notes = 0, []
    # No extra credit for rank 5 vs rank 25: on non-point-in-time data that difference is noise.
    if rank and rank <= n:
        pts += 1; notes.append(f"+1 in the top {n}")  # noqa: E702
    pos = sum(z > 0 for _, z in themes)
    if pos >= 4:
        pts += 1; notes.append(f"+1 positive on {pos}/5 factors")  # noqa: E702
    if not any(x["margin"] < NEAR for x in near):
        pts += 1; notes.append("+1 no gate near its limit")  # noqa: E702
    if reg["max_equity_exposure"] < 1:
        pts -= 1; notes.append("−1 risk-off market")  # noqa: E702
    if warn_checks:
        k = min(len(warn_checks), 2)
        pts -= k; notes.append(f"−{k} warning sign{'s' if k > 1 else ''} in filings/signals")  # noqa: E702
    if positives and action in ("BUY", "BUY AFTER", "HOLD"):
        pts += 1; notes.append("+1 " + positives[0].split(":")[0])  # noqa: E702
        why.append(positives[0][0].upper() + positives[0][1:])
    proven = validation.get("status") == "PASSED"
    # The backtest validates only the price factors (momentum, low volatility). Credit that evidence
    # only when those factors are what make this stock attractive; value/quality/yield are untested.
    tested = [r["themes"].get(k) for k in ("momentum", "low_vol") if r["themes"].get(k) is not None]
    tested_avg = sum(tested) / len(tested) if tested else None
    if proven and tested_avg is not None and tested_avg > 0:
        pts += 1; notes.append("+1 its strengths include the backtested price factors")  # noqa: E702
    elif proven:
        notes.append("no evidence point: ranked mainly on value/quality, which aren't backtested yet")
    conviction = "HIGH" if pts >= 4 else "MEDIUM" if pts >= 2 else "LOW"
    if not (proven and tested_avg and tested_avg > 0) and conviction == "HIGH":
        conviction = "MEDIUM"
        notes.append("capped at MEDIUM: no backtested edge behind this pick")

    weight = target_weight(r["ticker"], scored, market, reg["max_equity_exposure"]) if action in ("BUY", "BUY AFTER", "HOLD") and rank else None
    sell_if = [f"Rank drops below {buffer} at a monthly review"]
    sell_if += [f"{x['label']} {x['rule']} (now {x['value']})" for x in near]
    sell_if.append("Any other safety gate fails on new results")

    return {
        "action": action,
        "start_date": start.isoformat() if start and action == "BUY AFTER" else None,
        "sell_date": start.isoformat() if start and action == "HOLD" and rank and rank > buffer else None,
        "conviction": conviction if action in ("BUY", "BUY AFTER", "HOLD") else None,
        "conviction_points": notes,
        "conviction_score": pts if action in ("BUY", "BUY AFTER", "HOLD") else None,
        "evidence_scope": EVIDENCE_SCOPE,
        "position_size": weight,
        "position_note": None if weight is None else
        f"{weight:.1%} of the portfolio (inverse-volatility, capped at {PORTFOLIO['max_weight'][market]:.0%}"
        + (f", halved for the risk-off market)" if reg["max_equity_exposure"] < 1 else ")"),
        "why": why[:4],
        "risks": risks[:4],
        "sell_if": sell_if if action in ("BUY", "BUY AFTER", "HOLD") else [],
        "alternative": None if proven else ETF_ALTERNATIVE.get(market),
        "next_review": (today + timedelta(days=30)).isoformat(),
        "checks": checks,
        "signals": signals,
    }


def _pct(z: float) -> int:
    """z-score -> percentile (normal approximation, clipped to 1-99), for plain-English phrasing."""
    from statistics import NormalDist
    return min(99, max(1, round(NormalDist().cdf(z) * 100)))
