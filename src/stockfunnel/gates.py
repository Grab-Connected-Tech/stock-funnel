"""Stage 2 of the funnel: hard exclusion gates. Any FAIL rejects. Too many UNKNOWNs reject too
(fail closed): if we can't verify a company is safe, we don't buy it."""
from __future__ import annotations

from .policy import GATES as G

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"
TRADABILITY = {"history_1y", "min_price", "liquidity", "min_market_cap"}
FORENSIC = {"altman_z2", "beneish_m", "montier_c", "piotroski", "accruals", "cash_generation"}


def _check(value, ok) -> str:
    return UNKNOWN if value is None else PASS if ok(value) else FAIL


def evaluate(m: dict, market: str) -> dict:
    """m = merged price + fundamental metrics (+ 'vol_percentile' set cross-sectionally)."""
    fin = bool(m.get("is_financial") or False)
    checks = {
        # tradability
        "history_1y": _check(m.get("history_days"), lambda v: v >= G["min_history_days"]),
        "min_price": _check(m.get("price"), lambda v: v >= G["min_price"][market]),
        "liquidity": _check(m.get("adv_value_60d"), lambda v: v >= G["min_adv_value"][market]),
        "data_quality": _check(m.get("bad_print_days_1y"), lambda v: v == 0),
        "no_stale_trading": _check(m.get("zero_volume_days_60d"), lambda v: v <= G["max_zero_volume_days_60d"]),
        "min_market_cap": _check(m.get("market_cap"), lambda v: v >= G["min_market_cap"][market]),
        "not_lottery_stock": _check(m.get("vol_percentile"), lambda v: v <= G["max_vol_percentile"]),
        "positive_equity": _check(m.get("negative_equity"), lambda v: not bool(v)),  # numpy-bool safe
        "dilution": _check(m.get("net_issuance_1y"), lambda v: v <= G["max_net_issuance_1y"]),
    }
    if fin:
        # Lenders' operating cash flow swings with loan growth, so cash-flow/accrual/Piotroski
        # tests are meaningless; judge on returns instead. Insurers run structurally low ROA.
        if "insurance" in str(m.get("industry", "")).lower():
            checks["financial_roe"] = _check(m.get("roe"), lambda v: v >= G["insurer_min_roe"])
        else:
            checks["financial_roa"] = _check(m.get("roa"), lambda v: v >= G["financial_min_roa"])
    else:
        checks.update({
            "piotroski": _check(m.get("piotroski"), lambda v: v >= G["piotroski_min"]),
            "accruals": _check(m.get("accruals"), lambda v: v <= G["accruals_max"]),
            "cash_generation": _check(m.get("negative_cfo_years_3y"), lambda v: v <= G["max_negative_cfo_years_3y"]),
            # Regulated utilities carry high debt by design; Z'' wasn't estimated on them.
            "altman_z2": PASS if m.get("sector") == "Utilities" else
            _check(m.get("altman_z2"), lambda v: v >= G["altman_z2_min"]),
            "beneish_m": _check(m.get("beneish_m"), lambda v: v <= G["beneish_m_max"]),
            "montier_c": _check(m.get("montier_c"), lambda v: v <= G["montier_c_max"]),
            "leverage": _check(m.get("debt_to_equity"), lambda v: v <= G["max_debt_to_equity"]),
            "interest_coverage": _check(m.get("interest_coverage"), lambda v: v >= G["min_interest_coverage"]),
        })
    failed = [k for k, v in checks.items() if v == FAIL]
    unknown = [k for k, v in checks.items() if v == UNKNOWN]
    # Tradability and forensic unknowns are always fatal: we must know we can get in and out, and we
    # never buy a company whose accounts we couldn't screen. One unknown is tolerated only among the
    # remaining solvency items (e.g. interest coverage when the data omits interest expense).
    fatal_unknown = [k for k in unknown if k in TRADABILITY | FORENSIC]
    passed = not failed and not fatal_unknown and len(unknown) <= G["max_unknown_gates"]
    return {"passed": passed, "failed": failed, "unknown": unknown, "checks": checks}

