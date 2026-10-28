"""Stage 5: qualitative review by an LLM (or human), under asymmetric authority.

The reviewer can only *block or shrink* what the quant stage proposed. It can never cause a
trade, raise a size, or rescue a rejected stock. Malformed reviews count as rejections.
"""
from __future__ import annotations

from .policy import ACTIONS

VERDICTS = ("PROCEED", "WATCH_ONLY", "REJECT", "INSUFFICIENT_DATA")
VETO_FLAGS = (
    "ACCOUNTING_RED_FLAG", "GOVERNANCE_CONCERN", "PROMOTER_OR_INSIDER_RISK", "REGULATORY_OR_LEGAL_RISK",
    "MOAT_ERODING", "CYCLICAL_PEAK_EARNINGS", "CAPITAL_ALLOCATION_POOR", "DILUTION_RISK",
    "CUSTOMER_OR_SUPPLIER_CONCENTRATION", "DISRUPTION_RISK", "OUTSIDE_COMPETENCE", "NARRATIVE_ONLY_THESIS",
    "DATA_CONFLICT", "OTHER",
)
CHECKLIST = (  # Buffett/Munger, Akre, Fundsmith, Marcellus distilled
    "business_understandable", "durable_advantage", "management_trustworthy",
    "reinvestment_runway", "balance_sheet_resilient", "thesis_survives_inversion",
)
TRI = ("YES", "NO", "UNKNOWN")
SEVERITY = ("LOW", "MEDIUM", "HIGH")
SUPPORTS = ("BULL", "BEAR", "NEUTRAL")

SCHEMA = {
    "ticker": "string (must equal the packet ticker)",
    "verdict": list(VERDICTS),
    "veto_flags": f"array, subset of {list(VETO_FLAGS)}",
    "checklist": {k: list(TRI) for k in CHECKLIST},
    "premortem": "3-6 items: {cause: string<=200 chars, severity: LOW|MEDIUM|HIGH}. 'It is 3 years later and this lost 50%. Why?'",
    "kill_criteria": "2-5 items: {metric_id: a key from packet.facts starting 'm.', direction: FALLS_BELOW|RISES_ABOVE}",
    "evidence": "1+ items: {fact_id: a key from packet.facts, supports: BULL|BEAR|NEUTRAL}",
    "confidence": ["LOW", "MEDIUM"],  # no HIGH on purpose
    "narrative": "optional string <=800 chars; display only, never parsed",
}

INSTRUCTIONS = """You are a skeptical investment committee member reviewing ONE stock the quant funnel proposed.
Your job is to find reasons NOT to proceed. You cannot increase any position or override any rule.
- Use only the facts in the packet. Do not use remembered prices, ratios, or news you can't cite.
- Cite evidence by fact_id. Do not write numbers in the narrative that are not in the packet.
- Answer UNKNOWN rather than guess. Run the pre-mortem: assume it lost 50% in 3 years and explain why.
- Return ONLY a JSON object matching the schema."""


def validate(review: dict, packet: dict) -> list[str]:
    """Returns a list of problems; empty means valid."""
    errs = []
    facts = packet["facts"]
    allowed = {"ticker", "verdict", "veto_flags", "checklist", "premortem", "kill_criteria", "evidence", "confidence", "narrative"}
    if not isinstance(review, dict):
        return ["review is not an object"]
    if extra := set(review) - allowed:
        errs.append(f"unexpected fields: {sorted(extra)}")
    if review.get("ticker") != packet["ticker"]:
        errs.append("ticker mismatch")
    if review.get("verdict") not in VERDICTS:
        errs.append("verdict not in enum")
    flags = review.get("veto_flags")
    if not isinstance(flags, list) or any(f not in VETO_FLAGS for f in flags):
        errs.append("veto_flags invalid")
    cl = review.get("checklist")
    if not isinstance(cl, dict) or set(cl) != set(CHECKLIST) or any(v not in TRI for v in cl.values()):
        errs.append("checklist must have exactly the required keys with YES/NO/UNKNOWN")
    pm = review.get("premortem")
    if not isinstance(pm, list) or not 3 <= len(pm) <= 6 or any(
            not isinstance(p, dict) or p.get("severity") not in SEVERITY or not str(p.get("cause", "")).strip() for p in pm):
        errs.append("premortem needs 3-6 {cause, severity} items")
    kc = review.get("kill_criteria")
    if not isinstance(kc, list) or not 2 <= len(kc) <= 5 or any(
            not isinstance(k, dict) or k.get("metric_id") not in facts or not str(k.get("metric_id")).startswith("m.")
            or k.get("direction") not in ("FALLS_BELOW", "RISES_ABOVE") for k in kc):
        errs.append("kill_criteria needs 2-5 items referencing packet metric ids")
    ev = review.get("evidence")
    if not isinstance(ev, list) or not ev or any(
            not isinstance(e, dict) or e.get("fact_id") not in facts or e.get("supports") not in SUPPORTS for e in ev):
        errs.append("evidence must cite existing fact_ids")
    if review.get("confidence") not in ("LOW", "MEDIUM"):
        errs.append("confidence must be LOW or MEDIUM")
    if len(str(review.get("narrative", ""))) > 800:
        errs.append("narrative too long")
    return errs


def cap_from_review(review: dict, packet: dict) -> tuple[str, list[str], bool]:
    """Max action this review allows, reasons, and whether a human must look."""
    errs = validate(review, packet)
    if errs:
        return "HOLD", [f"invalid review: {e}" for e in errs], False
    reasons, human = [], False
    cap = "BUY"
    if review["verdict"] != "PROCEED":
        cap = "WATCH" if review["verdict"] == "WATCH_ONLY" else "HOLD"
        reasons.append(f"verdict {review['verdict']}")
    if review["veto_flags"]:
        cap = _min(cap, "WATCH")
        reasons.append(f"veto flags: {', '.join(review['veto_flags'])}")
        if {"ACCOUNTING_RED_FLAG", "GOVERNANCE_CONCERN"} & set(review["veto_flags"]):
            cap, human = "HOLD", True
    if any(p["severity"] == "HIGH" for p in review["premortem"]):
        cap = _min(cap, "WATCH")
        human = True
        reasons.append("high-severity pre-mortem risk")
    answers = review["checklist"].values()
    if "NO" in answers or list(answers).count("UNKNOWN") > 2:
        cap = _min(cap, "WATCH")
        reasons.append("checklist not satisfied")
    return cap, reasons, human


def _min(a: str, b: str) -> str:
    return a if ACTIONS.index(a) <= ACTIONS.index(b) else b


def combine(quant_action: str, reviews: list[dict], packet: dict, held: bool, require_review_for_buy: bool = True) -> dict:
    """Final action = most restrictive of the quant action and every review's cap.
    Reviews never cause trades: caps only apply to BUY (incl. top-ups); SELL/TRIM/HOLD pass through."""
    if quant_action != "BUY":
        return {"action": quant_action, "review_reasons": [], "human_review": False}
    if not reviews:
        if require_review_for_buy:
            return {"action": "HOLD" if held else "WATCH", "review_reasons": ["pending review"], "human_review": False,
                    "pending_review": True}
        return {"action": "BUY", "review_reasons": [], "human_review": False}
    cap, reasons, human = "BUY", [], False
    for r in reviews:
        c, rs, h = cap_from_review(r, packet)
        cap, human = _min(cap, c), human or h
        reasons += rs
    final = _min(quant_action, cap)
    if final == "WATCH" and held:
        final = "HOLD"
    if final == "HOLD" and not held:
        final = "WATCH"
    return {"action": final, "review_reasons": sorted(set(reasons)), "human_review": human}
