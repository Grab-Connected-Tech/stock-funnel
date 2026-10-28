"""Stage 3: multi-factor composite. Sector-neutral, winsorized z-scores averaged within themes,
then a frozen weighted sum (docs/research/factors.md)."""
from __future__ import annotations

import pandas as pd

from .policy import MIN_THEMES, MISSING_THEME_Z, WEIGHTS

# (metric, sign). Sign -1 = lower is better.
THEMES = {
    "value": [("earnings_yield", 1), ("fcf_to_ev", 1), ("earnings_to_price", 1), ("sales_to_ev", 1)],
    "quality": [("gross_profitability", 1), ("roic", 1), ("roe", 1), ("debt_to_equity", -1), ("earnings_variability", -1)],
    "momentum": [("mom_12_1_risk_adj", 1), ("mom_6_1_risk_adj", 1)],
    "low_vol": [("vol_1y", -1)],
    "yield": [("shareholder_yield", 1)],
}
# Financials: EBIT/EV, sales/EV, GP/A and D/E are meaningless for banks/insurers.
FINANCIAL_OVERRIDES = {"value": [("earnings_to_price", 1), ("book_to_price", 1)],
                       "quality": [("roe", 1), ("roa", 1), ("earnings_variability", -1)]}


def _z(s: pd.Series) -> pd.Series:
    s = s.astype(float)
    if s.notna().sum() < 3 or s.std() == 0:
        return s * 0.0
    s = s.clip(s.quantile(0.01), s.quantile(0.99))
    return ((s - s.mean()) / s.std()).clip(-3, 3)


def _sector_z(df: pd.DataFrame, col: str) -> pd.Series:
    """z within sector when the sector has >= 5 names, else within the whole universe."""
    whole = _z(df[col])
    by_sector = df.groupby("sector")[col].transform(lambda s: _z(s) if s.notna().sum() >= 5 else s * float("nan"))
    return by_sector.fillna(whole).where(df[col].notna())


def composite(df: pd.DataFrame, market: str, momentum_crash_guard: bool = False) -> pd.DataFrame:
    """df: one row per ticker (index) with metric columns + 'sector' + 'is_financial'.
    Returns df with theme_* columns, 'score', 'rank' (1 = best)."""
    out = df.copy()
    weights = dict(WEIGHTS[market])
    if momentum_crash_guard:  # Daniel-Moskowitz panic state: move half of momentum to quality
        weights["quality"] += weights["momentum"] / 2
        weights["momentum"] /= 2

    for theme, comps in THEMES.items():
        z = {}
        for fin in (False, True):
            rows = out["is_financial"].fillna(False) == fin
            use = FINANCIAL_OVERRIDES.get(theme, comps) if fin else comps
            sub = out[rows]
            parts = [_sector_z(sub, c) * sign for c, sign in use if c in sub]
            z[fin] = pd.concat(parts, axis=1).mean(axis=1) if parts else pd.Series(dtype=float, index=sub.index)
        out[f"theme_{theme}"] = _z(pd.concat(z.values()).reindex(out.index))

    theme_cols = [f"theme_{t}" for t in THEMES]
    out["themes_available"] = out[theme_cols].notna().sum(axis=1)
    out["score"] = sum(out[f"theme_{t}"].fillna(MISSING_THEME_Z) * w for t, w in weights.items())
    out.loc[out["themes_available"] < MIN_THEMES, "score"] = float("nan")  # too little evidence to rank
    out["rank"] = out["score"].rank(ascending=False, method="first")
    return out.sort_values("rank")
