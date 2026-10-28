"""Stage 4: turn ranks into target weights and quant actions. Pure function of its inputs."""
from __future__ import annotations

from datetime import date

import pandas as pd

from .policy import PORTFOLIO as P


def capped_weights(raw: pd.Series, sectors: pd.Series, cap: float, sector_cap: float, total: float = 1.0) -> pd.Series:
    """Normalize raw scores to `total`, enforcing per-name and per-sector caps by redistribution.
    Whatever can't be placed without breaking a cap stays in cash."""
    w = raw / raw.sum() * total
    for _ in range(50):
        over_name = w > cap + 1e-12
        sec_tot = w.groupby(sectors).transform("sum")
        over_sec = sec_tot > sector_cap + 1e-12
        if not over_name.any() and not over_sec.any():
            break
        w[over_name] = cap
        w[over_sec] = w[over_sec] * sector_cap / sec_tot[over_sec]
        free = ~(w >= cap - 1e-12) & ~(w.groupby(sectors).transform("sum") >= sector_cap - 1e-12)
        room = total - w.sum()
        if room <= 1e-9 or not free.any():
            break
        w[free] += room * raw[free] / raw[free].sum()
    return w


def plan(scored: pd.DataFrame, rejected: dict[str, list[str]], market: str, regime: dict,
         holdings: dict[str, float], watchlist: dict[str, str], capital: float, today: date | None = None) -> dict:
    """
    scored: gate-passing tickers with 'rank', 'vol_1y', 'sector', 'adv_value_60d', 'ret_1m', 'score'.
    rejected: ticker -> failed gate names (from gates stage).
    holdings: ticker -> current weight. watchlist: ticker -> ISO date first seen.
    Returns targets, per-ticker actions with reasons, and watchlist updates.
    """
    today = today or date.today()
    n, buffer = P["n_holdings"][market], P["hold_until_rank"][market]
    actions: dict[str, dict] = {}

    # 1. Existing holdings: sell only on rule (gate failure or rank outside the buffer).
    keep = []
    for t in holdings:
        if t in rejected:
            actions[t] = {"action": "SELL", "reason": f"failed gates: {', '.join(rejected[t]) or 'data unverifiable'}"}
        elif t not in scored.index or pd.isna(scored.at[t, "rank"]):
            actions[t] = {"action": "SELL", "reason": "no longer rankable (left universe or insufficient data)"}
        elif scored.at[t, "rank"] > buffer:
            actions[t] = {"action": "SELL", "reason": f"rank {int(scored.at[t, 'rank'])} outside hold buffer {buffer}"}
        else:
            keep.append(t)

    # 2. New candidates in rank order; cooling-off and anti-chasing rules apply.
    new_watch = {}
    candidates = []
    for t in scored.index[scored["rank"] <= n]:
        if t in keep or len(keep) + len(candidates) >= n:
            continue
        first_seen = watchlist.get(t)
        waited = (today - date.fromisoformat(first_seen)).days if first_seen else 0
        if not first_seen:
            new_watch[t] = today.isoformat()
        if waited < P["cooling_off_days"]:
            actions[t] = {"action": "WATCH", "reason": f"cooling-off: {waited}/{P['cooling_off_days']} days on watchlist"}
        elif scored.at[t, "ret_1m"] > P["max_1m_return_for_buy"]:
            actions[t] = {"action": "WATCH", "reason": f"anti-chasing: up {scored.at[t, 'ret_1m']:.0%} in 1 month"}
        else:
            candidates.append(t)

    # 3. Weights: inverse volatility, capped, scaled by regime exposure, limited by liquidity.
    book = keep + candidates
    targets = pd.Series(dtype=float)
    if book:
        sub = scored.loc[book]
        targets = capped_weights(1 / sub["vol_1y"], sub["sector"], P["max_weight"][market],
                                 P["max_sector_weight"], total=regime["max_equity_exposure"])
        liq_cap = P["max_adv_fraction"] * sub["adv_value_60d"] / capital
        targets = targets.clip(upper=liq_cap)
        targets = targets[targets >= P["min_weight"]]

    for t in book:
        tgt, cur = float(targets.get(t, 0.0)), holdings.get(t, 0.0)
        if tgt == 0.0:
            actions[t] = {"action": "SELL" if cur else "WATCH", "reason": "weight below minimum after caps/liquidity"}
        elif cur == 0.0:
            actions[t] = {"action": "BUY", "reason": f"rank {int(scored.at[t, 'rank'])}, passed all gates and cooling-off"}
        elif tgt > cur * (1 + P["drift_rebalance"]):
            actions[t] = {"action": "BUY", "reason": f"top-up: target {tgt:.1%} vs current {cur:.1%}"}
        elif tgt < cur * (1 - P["drift_rebalance"]):
            actions[t] = {"action": "TRIM", "reason": f"trim: target {tgt:.1%} vs current {cur:.1%}"}
        else:
            actions[t] = {"action": "HOLD", "reason": f"within drift band (target {tgt:.1%}, current {cur:.1%})"}
        actions[t]["target_weight"] = round(tgt, 4) if actions[t]["action"] not in ("HOLD",) else round(cur, 4)

    for t, a in actions.items():
        a.setdefault("target_weight", 0.0)

    return {
        "actions": actions,
        "equity_exposure": round(float(sum(a["target_weight"] for a in actions.values())), 4),
        "cash_weight": round(1 - float(sum(a["target_weight"] for a in actions.values())), 4),
        "watchlist_additions": new_watch,
    }
