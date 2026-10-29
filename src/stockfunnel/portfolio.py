"""Stage 4: turn ranks into target weights and quant actions. Pure function of its inputs."""
from __future__ import annotations

from datetime import date

import pandas as pd

from .policy import PORTFOLIO as P


def capped_weights(raw: pd.Series, sectors: pd.Series, cap: float, sector_cap: float, total: float = 1.0) -> pd.Series:
    """Normalize raw scores to `total`, enforcing per-name and per-sector caps by redistribution.
    Whatever can't be placed without breaking a cap stays in cash."""
    w = raw / raw.sum() * total
    for _ in range(200):
        w = w.clip(upper=cap)
        sec_tot = w.groupby(sectors).transform("sum")  # after name caps
        w = w * (sector_cap / sec_tot).clip(upper=1.0)
        room = total - w.sum()
        sec_tot = w.groupby(sectors).transform("sum")
        free = (w < cap - 1e-9) & (sec_tot < sector_cap - 1e-9)
        if room <= 1e-9 or not free.any():
            break
        w[free] += room * raw[free] / raw[free].sum()  # overshoot is clipped next pass
    w = w.clip(upper=cap)  # final enforcement: caps are guaranteed, excess becomes cash
    return w * (sector_cap / w.groupby(sectors).transform("sum")).clip(upper=1.0)


def tax_deferral_days(market: str, opened: str | None, gain: float | None, as_of: date) -> int:
    """Days to wait before a rank-based sell so a winner qualifies for long-term capital gains (0 = sell now)."""
    window = P["ltcg_defer_window_days"][market]
    if not window or not opened or gain is None or gain <= 0:
        return 0
    left = 366 - (as_of - date.fromisoformat(opened)).days
    return left if 0 < left <= window else 0


def plan(scored: pd.DataFrame, rejected: dict[str, list[str]], market: str, regime: dict,
         holdings: dict[str, float], watchlist: dict[str, str], vetoes: dict[str, dict], capital: float,
         as_of: date, last_rebalance: str | None = None, positions_meta: dict[str, dict] | None = None) -> dict:
    """
    scored: gate-passing tickers with 'rank', 'vol_1y', 'sector', 'adv_value_60d', 'ret_1m', 'score'.
    rejected: ticker -> failed gate names. holdings: ticker -> current weight.
    watchlist: ticker -> ISO date it (continuously) entered the buy zone. vetoes: ticker -> standing veto.
    Returns per-ticker actions with reasons and the replacement watchlist.
    """
    n, buffer = P["n_holdings"][market], P["hold_until_rank"][market]
    actions: dict[str, dict] = {}
    rebalance_due = last_rebalance is None or (as_of - date.fromisoformat(last_rebalance)).days >= P["min_days_between_rebalances"]

    # 1. Existing holdings: sell only on rule (gate failure or rank outside the buffer). Gate SELLs ignore cadence.
    keep = []
    for t in holdings:
        hard_fails = [r for r in rejected.get(t, []) if not r.endswith("?")]
        if hard_fails:
            actions[t] = {"action": "SELL", "reason": f"failed gates: {', '.join(hard_fails)}"}
        elif t in rejected:  # only unknowns, e.g. a data outage: don't force-sell on missing data
            actions[t] = {"action": "HOLD", "reason": f"data unverifiable ({', '.join(rejected[t])}); holding, no adds"}
        elif t not in scored.index or pd.isna(scored.at[t, "rank"]):
            actions[t] = {"action": "SELL", "reason": "no longer rankable (left universe or insufficient data)"}
        elif scored.at[t, "rank"] > buffer and rebalance_due:
            meta = (positions_meta or {}).get(t, {})
            wait = tax_deferral_days(market, meta.get("opened"), meta.get("gain"), as_of)
            if wait:  # safety-gate sells above never wait; only this rank-based one does
                actions[t] = {"action": "HOLD", "reason": f"rank {int(scored.at[t, 'rank'])} outside buffer, but selling in "
                              f"{wait} days qualifies for long-term tax (12.5% vs 20%)",
                              "target_weight": round(holdings[t], 4)}  # held as-is: never re-weighted or topped up
            else:
                actions[t] = {"action": "SELL", "reason": f"rank {int(scored.at[t, 'rank'])} outside hold buffer {buffer}"}
        else:
            keep.append(t)

    # 2. New candidates in rank order. Cooling-off counts *consecutive* days in the buy zone:
    #    a name that drops out loses its place on the watchlist.
    top = [t for t in scored.index[scored["rank"] <= n] if t not in keep]
    new_watchlist = {t: watchlist.get(t, as_of.isoformat()) for t in top}
    candidates = []
    for t in top:
        if len(keep) + len(candidates) >= n:
            break
        waited = (as_of - date.fromisoformat(new_watchlist[t])).days
        if t in vetoes:
            actions[t] = {"action": "WATCH", "reason": f"standing veto since {vetoes[t]['date']}: {vetoes[t]['reason']}"}
        elif waited < P["cooling_off_days"]:
            actions[t] = {"action": "WATCH", "reason": f"cooling-off: {waited}/{P['cooling_off_days']} days in buy zone"}
        elif scored.at[t, "ret_1m"] > P["max_1m_return_for_buy"]:
            actions[t] = {"action": "WATCH", "reason": f"anti-chasing: up {scored.at[t, 'ret_1m']:.0%} in 1 month"}
        elif not rebalance_due:
            actions[t] = {"action": "WATCH", "reason": f"next rebalance after {P['min_days_between_rebalances']} days"}
        else:
            candidates.append(t)

    # 3. Weights: inverse vol at full exposure -> caps -> minimum weight -> liquidity, THEN scale
    #    by the regime. (Scaling first would push names under the minimum and liquidate the book.)
    book = keep + candidates
    targets = pd.Series(dtype=float)
    if book:
        sub = scored.loc[book]
        targets = capped_weights(1 / sub["vol_1y"], sub["sector"], P["max_weight"][market], P["max_sector_weight"])
        targets = targets[targets >= P["min_weight"]]
        targets = targets.clip(upper=P["max_adv_fraction"] * sub["adv_value_60d"] / capital) * regime["max_equity_exposure"]

    for t in book:
        tgt, cur = float(targets.get(t, 0.0)), holdings.get(t, 0.0)
        if tgt == 0.0:
            actions[t] = {"action": "SELL" if cur and rebalance_due else "HOLD" if cur else "WATCH",
                          "reason": "weight below minimum after caps/liquidity"}
        elif cur == 0.0:
            actions[t] = {"action": "BUY", "reason": f"rank {int(scored.at[t, 'rank'])}, passed all gates and cooling-off"}
        elif not rebalance_due:
            actions[t] = {"action": "HOLD", "reason": "between rebalance dates"}
        elif tgt > cur * (1 + P["drift_rebalance"]) and t not in vetoes:
            actions[t] = {"action": "BUY", "reason": f"top-up: target {tgt:.1%} vs current {cur:.1%}"}
        elif tgt < cur * (1 - P["drift_rebalance"]):
            actions[t] = {"action": "TRIM", "reason": f"trim: target {tgt:.1%} vs current {cur:.1%}"}
        else:
            actions[t] = {"action": "HOLD", "reason": f"within drift band (target {tgt:.1%}, current {cur:.1%})"}
        actions[t]["target_weight"] = round(cur if actions[t]["action"] == "HOLD" else tgt, 4)

    for a in actions.values():
        a.setdefault("target_weight", 0.0)
    return {"actions": actions, "watchlist": new_watchlist, "rebalance_due": rebalance_due}
