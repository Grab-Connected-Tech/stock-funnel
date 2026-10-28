"""Frozen policy. Every threshold in one place, each traceable to docs/research/.

Rules for changing this file: never during a decision, never to make a specific stock pass,
and only after re-running `stockfunnel validate`. The MCP server exposes no way to edit it.
"""
from types import MappingProxyType as Frozen

GATES = Frozen({
    "min_history_days": 252,           # >= 1y listed
    "min_price": {"US": 5.0, "IN": 50.0},
    "min_adv_value": {"US": 10e6, "IN": 10e7},   # 60d median daily traded value: $10M / ₹10 cr
    "max_zero_volume_days_60d": 2,
    "min_market_cap": {"US": 2e9, "IN": 5e10},   # $2B / ₹5,000 cr
    "altman_z2_min": 1.1,              # distress zone (non-financials)
    "beneish_m_max": -1.78,            # manipulation flag
    "piotroski_min": 4,                # F <= 3 excluded
    "montier_c_max": 4,                # C >= 5 excluded
    "accruals_max": 0.10,              # (NI-CFO)/avg TA
    "max_negative_cfo_years_3y": 1,
    "max_net_issuance_1y": 0.10,       # dilution
    "financial_min_roa": 0.005,
    "insurer_min_roe": 0.08,
    "max_vol_percentile": 0.90,        # drop top-decile volatility (lottery stocks)
    "max_debt_to_equity": 2.0,         # non-financials
    "min_interest_coverage": 3.0,
    "max_unknown_gates": 1,            # fail closed: can't verify => can't buy
})

# Theme weights (frozen, never optimized). Revisions/SUE omitted: no free estimate history;
# its 5% is spread proportionally.
WEIGHTS = Frozen({
    "US": Frozen({"value": 0.26, "quality": 0.26, "momentum": 0.26, "low_vol": 0.11, "yield": 0.11}),
    "IN": Frozen({"value": 0.16, "quality": 0.32, "momentum": 0.31, "low_vol": 0.16, "yield": 0.05}),
})
MISSING_THEME_Z = -0.5   # a theme we can't compute counts as mildly bad, never neutral
MIN_THEMES = 4

PORTFOLIO = Frozen({
    "n_holdings": {"US": 30, "IN": 30},
    "hold_until_rank": {"US": 75, "IN": 60},   # rank buffer: cuts turnover (Novy-Marx & Velikov)
    "max_weight": {"US": 0.06, "IN": 0.07},
    "min_weight": 0.015,
    "max_sector_weight": 0.25,
    "max_adv_fraction": 0.02,          # position <= 2% of daily traded value
    "cooling_off_days": 10,            # a name must sit on the watchlist before the first buy
    "max_1m_return_for_buy": 0.25,     # no chasing
    "drift_rebalance": 0.30,           # trade only if weight drifts > ±30% relative
    "max_annual_turnover": 2.0,        # buy-side value / NAV per trailing year, incl. initial build-out (~1.0)
    "min_days_between_rebalances": 25, # monthly cadence = what validate.py backtests (gate SELLs exempt)
    "max_trades_per_month": 20,
})

RISK = Frozen({
    "pause_buys_drawdown": -0.15,
    "halt_autopilot_drawdown": -0.25,  # requires human reset
    "max_daily_loss": -0.03,
    "max_order_fraction_nav": 0.08,
    "max_data_age_days": 4,            # stale data blocks trading (weekends + holiday)
})

# Ordered actions; the LLM may only move an action to the left.
ACTIONS = ("SELL", "TRIM", "HOLD", "WATCH", "BUY")
