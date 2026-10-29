"""The funnel, end to end:

  1 Universe -> 2 Regime -> 3 Tradability prefilter -> 4 Forensic & solvency gates
  -> 5 Multi-factor score -> 6 Portfolio rules -> 7 LLM/human review (veto-only) -> 8 Execute (autopilot) / report (manual)

Everything up to step 6 is deterministic code. Step 7 can only block. Step 8 re-checks risk.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from . import broker, checks, data, gates, journal, metrics, portfolio, regime, review, score
from .policy import GATES

REVIEW_DIR = Path("reviews")
HIDDEN_FROM_REVIEW = {"analyst_rec_mean", "price", "avg_cost"}  # anchoring / herding inputs


def _fund_metrics(t: str) -> dict:
    for attempt in range(3):
        try:
            return metrics.fundamental_metrics(data.fundamentals(t))
        except Exception as e:  # yfinance rate limits / missing data
            if "Rate" in type(e).__name__ or "429" in str(e):
                time.sleep(10 * (attempt + 1))
                continue
            return {"data_error": str(e)[:120]}
    return {"data_error": "rate limited"}


def analyze_universe(universe: str, limit: int | None = None, extra: list[str] | None = None,
                     include: list[str] | None = None) -> dict:
    """extra: tickers priced but not ranked (e.g. holdings that left the universe).
    include: non-member tickers to gate and rank alongside the universe (single-stock analysis)."""
    market, tickers = data.universe(universe)
    tickers = tickers[:limit] if limit else tickers
    tickers = tickers + [t for t in include or [] if t not in tickers]
    reg = regime.assess(market)
    px = data.prices(sorted(set(tickers) | set(extra or [])), period="2y")
    raw_close, vol = px["close"], px["volume"]
    last, last_date = data.last_prices(raw_close)   # trade at real quotes
    close, suspect = data.clean(raw_close)          # compute signals on repaired history

    rows = {}
    for t in tickers:
        if t not in close:
            rows[t] = {"history_days": 0}
            continue
        rows[t] = metrics.price_metrics(close[t], vol[t]) | {"bad_print_days_1y": int(suspect[t].iloc[-252:].sum())}
    df = pd.DataFrame.from_dict(rows, orient="index")
    df["vol_percentile"] = df["vol_1y"].rank(pct=True) if "vol_1y" in df else np.nan

    # Cheap price-only prefilter before spending API calls on fundamentals.
    pre = (df["history_days"] >= GATES["min_history_days"]) \
        & (df.get("price", 0) >= GATES["min_price"][market]) \
        & (df.get("adv_value_60d", 0) >= GATES["min_adv_value"][market])
    fund = {t: _fund_metrics(t) for t in df.index[pre.fillna(False)]}
    if fund:
        df = df.join(pd.DataFrame.from_dict(fund, orient="index"), how="left")

    results, rejected = {}, {}
    for t, row in df.iterrows():
        m = {k: (None if isinstance(v, float) and np.isnan(v) else v) for k, v in row.to_dict().items()}
        g = gates.evaluate(m, market)
        results[t] = g
        if not g["passed"]:
            rejected[t] = g["failed"] + [f"{u}?" for u in g["unknown"]]

    passed = df.loc[[t for t in df.index if results[t]["passed"]]].copy()
    for flag in ("is_financial", "is_reit"):
        passed[flag] = passed[flag].fillna(False).astype(bool) if flag in passed else False
    scored = (score.composite(passed, market, reg["momentum_crash_guard"]) if len(passed)
              else passed.assign(rank=pd.Series(dtype=float), score=pd.Series(dtype=float)))  # nothing passed
    return {"market": market, "universe": universe, "as_of": str(last_date), "regime": reg,
            "metrics": df, "gates": results, "rejected": rejected, "scored": scored,
            "last_prices": last, "last_date": last_date,
            "snapshot_sha": hashlib.sha256(pd.util.hash_pandas_object(df.astype(str), index=True).values.tobytes()
                                           + json.dumps(last, sort_keys=True).encode()).hexdigest()[:16]}


def review_packet(t: str, a: dict) -> dict:
    """Everything a reviewer may see about one ticker, as citable facts. No prices/P&L/analyst ratings."""
    m = a["metrics"].loc[t].to_dict()
    facts = {f"m.{k}": (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()
             if k not in HIDDEN_FROM_REVIEW and v is not None and not (isinstance(v, float) and np.isnan(v))}
    facts |= {f"g.{k}": v for k, v in a["gates"][t]["checks"].items()}
    if t in a["scored"].index:
        s = a["scored"].loc[t]
        facts |= {f"s.{c}": round(float(s[c]), 3) for c in s.index if c.startswith("theme_") and pd.notna(s[c])}
        facts["s.rank"] = int(s["rank"])
        facts["s.universe_size_after_gates"] = len(a["scored"])
    facts |= {f"r.{k}": v for k, v in a["regime"].items()}
    return {"ticker": t, "market": a["market"], "as_of": a["as_of"], "facts": facts,
            "not_covered": checks.NOT_COVERED[a["market"]],
            "kill_metric_ids": sorted(k for k in facts if k.startswith("m.") and k[2:] in review.DECISION_METRICS),
            "instructions": review.INSTRUCTIONS, "schema": review.SCHEMA}


REVIEW_WINDOW_DAYS = 5  # a review stays usable across a data-date rollover (weekend / late submission)


def load_reviews(market: str, as_of: str, t: str) -> list[dict]:
    """Reviews for this ticker filed for any data date in the last few days. Each is re-validated
    against today's packet, so a review citing facts that no longer exist simply turns invalid."""
    out, end = [], date.fromisoformat(as_of)
    for d in range(REVIEW_WINDOW_DAYS + 1):
        p = REVIEW_DIR / market / (end - timedelta(days=d)).isoformat() / f"{t}.json"
        if not p.exists():
            continue
        try:
            got = json.loads(p.read_text())
        except json.JSONDecodeError:
            got = [{"invalid": True}]  # counts as an invalid (blocking) review
        out += got if isinstance(got, list) else [got]
    return out


def save_review(market: str, as_of: str, t: str, rev: dict) -> list[str]:
    p = REVIEW_DIR / market / as_of / f"{t}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(p.read_text()) if p.exists() else []
    existing.append(rev)
    p.write_text(json.dumps(existing, indent=2))
    return existing


def _positions_meta(state: dict, last: dict) -> dict[str, dict]:
    return {t: {"opened": p.get("opened"), "gain": (last[t] / p["avg_cost"] - 1) if t in last and p["avg_cost"] else None}
            for t, p in state["positions"].items()}


DEFAULT_PEERS = {"US": "sp500", "IN": "nifty200"}


def analyze_stock(ticker: str, universe: str | None = None) -> dict:
    """One stock, judged exactly as the funnel would: gated and ranked against its peer universe.
    Read-only: never touches the book, watchlist or vetoes."""
    market = data.market_of(ticker)
    universe = universe or DEFAULT_PEERS[market]
    a = analyze_universe(universe, include=[ticker])
    if ticker not in a["metrics"].index or a["metrics"].at[ticker, "history_days"] == 0:
        return {"ticker": ticker, "verdict": "NO DATA", "detail": "no price history (check the symbol; India needs .NS)"}
    return stock_report(ticker, a, broker.load(market))


def stock_report(ticker: str, a: dict, st: dict, log: bool = True) -> dict:
    """The single place a stock's verdict and recommendation are computed. Used by analyze_stock AND
    run, so the same stock on the same data always gets the same answer."""
    from .policy import PORTFOLIO
    market, universe = a["market"], a["universe"]
    members = data.universe(universe)[1]
    g, sc = a["gates"][ticker], a["scored"]
    n, buffer = PORTFOLIO["n_holdings"][market], PORTFOLIO["hold_until_rank"][market]
    rank = int(sc.at[ticker, "rank"]) if ticker in sc.index and pd.notna(sc.at[ticker, "rank"]) else None
    if g["failed"]:
        verdict, detail = "REJECT", f"fails hard gates: {', '.join(g['failed'])}"
    elif not g["passed"]:
        verdict, detail = "REJECT", f"can't verify: {', '.join(g['unknown'])} (fail-closed)"
    elif rank is None:
        verdict, detail = "REJECT", "too little data to score"
    elif rank <= n:
        verdict, detail = "BUY CANDIDATE", f"rank {rank}/{len(sc)} in {universe}: inside the top {n}"
    elif rank <= buffer:
        verdict, detail = "HOLD ZONE", f"rank {rank}/{len(sc)}: fine to keep if owned (buffer {buffer}), not a new buy"
    else:
        verdict, detail = "PASS (not a buy)", f"passes gates but ranks {rank}/{len(sc)}; better candidates exist"
    m = a["metrics"].loc[ticker]
    pick = ["name", "sector", "market_cap", "pe", "roe", "roic", "debt_to_equity", "piotroski", "altman_z2", "beneish_m",
            "montier_c", "earnings_yield", "fcf_yield", "mom_12_1", "vol_1y", "max_dd_1y"]
    out = {
        "ticker": ticker, "as_of": a["as_of"], "peer_universe": universe, "member": ticker in members,
        "verdict": verdict, "detail": detail,
        "rank": rank, "ranked_peers": len(sc),
        "score": None if rank is None else round(float(sc.at[ticker, "score"]), 3),
        "themes": {} if rank is None else {c[6:]: round(float(sc.at[ticker, c]), 2) for c in sc.columns
                                           if c.startswith("theme_") and pd.notna(sc.at[ticker, c])},
        "gates": g, "regime": a["regime"],
        "metrics": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()
                    if k in pick and not (isinstance(v, float) and np.isnan(v))},
        "your_book": {"held": ticker in st["positions"], "watchlist_since": st["watchlist"].get(ticker),
                      "standing_veto": st.get("vetoes", {}).get(ticker),
                      **_positions_meta(st, a["last_prices"]).get(ticker, {})},
        "not_covered": checks.NOT_COVERED[market],
        "evidence": latest_validation(market),
    }
    from . import recommend
    full = {k: (None if isinstance(v, float) and np.isnan(v) else v) for k, v in m.items()}
    from . import scorecard, signals
    sig = signals.run(ticker, a["metrics"], a["as_of"])
    out["recommendation"] = recommend.build(out, full, sc, latest_validation(market), checks.run(ticker), signals=sig)
    if log:
        scorecard.log(market, ticker, a["as_of"], out["recommendation"]["action"], a["last_prices"].get(ticker), "analyze",
                      out["recommendation"]["conviction"], {k: v["status"] for k, v in sig.items()})
    return out


def record_review(universe: str, ticker: str, rev, reviewer_id: str) -> dict:
    """Single entry point for reviews (CLI and MCP). The *caller* stamps reviewer_id, so one agent
    can't pose as two reviewers. A valid REJECT / veto flag / high-severity pre-mortem becomes a
    standing veto immediately, whatever the ticker's current action."""
    if universe not in data.UNIVERSES:
        return {"valid": False, "errors": [f"unknown universe; choose one of {sorted(data.UNIVERSES)}"]}
    an = analyze_universe(universe)
    if ticker not in an["metrics"].index:
        return {"valid": False, "errors": [f"{ticker} is not in {universe}"]}
    rev = rev | {"reviewer_id": reviewer_id} if isinstance(rev, dict) else {"unparseable": str(rev)[:500]}
    packet = review_packet(ticker, an)
    errs = review.validate(rev, packet)
    save_review(an["market"], an["as_of"], ticker, rev)
    journal.record("review", {"ticker": ticker, "as_of": an["as_of"], "valid": not errs, "review": rev})
    veto = review.standing_veto(rev, packet)
    if veto:
        state = broker.load(an["market"])
        if (broker.STATE_DIR / f"{an['market']}.json").exists() and not broker.tampered(state):
            state.setdefault("vetoes", {}).setdefault(ticker, {"date": an["as_of"], "reason": veto})
            broker.save(state)
            journal.record("veto", {"market": an["market"], "ticker": ticker, "reason": veto})
    return {"valid": not errs, "errors": errs, "standing_veto": veto,
            "note": "invalid reviews are stored and block the buy; a BUY needs 2 valid PROCEEDs from distinct reviewers"}


def run(universe: str, mode: str = "manual", capital: float | None = None, limit: int | None = None,
        require_review: bool = True, today: date | None = None) -> dict:
    if mode not in ("manual", "autopilot"):
        raise ValueError("mode must be manual or autopilot")
    # "Official" runs (named universe, no limit, matching the book's locked universe) may change state.
    # Anything else is a what-if: it reports but never touches the book, watchlist or vetoes.
    market = data.UNIVERSES[universe][0] if universe in data.UNIVERSES else data.universe(universe)[0]
    state = broker.load(market, capital)
    official = universe in data.UNIVERSES and not limit and state.get("universe") in (None, universe)
    if mode == "autopilot":
        if not official:
            raise ValueError(f"autopilot needs a named universe, no --limit, matching the book's lock "
                             f"({state.get('universe') or 'unlocked'})")
    if official:
        if not journal.verify():
            raise RuntimeError("journal hash chain is broken; refusing (inspect journal/decisions.jsonl)")
        if broker.tampered(state):
            raise RuntimeError(f"state/{market}.json changed outside stockfunnel (hash mismatch with journal); refusing")
        state["universe"] = universe

    a = analyze_universe(universe, limit, extra=list(state["positions"]))
    last, as_of = a["last_prices"], a["last_date"]
    risk = broker.risk_status(state, last, as_of)
    held = broker.weights(state, last)
    vetoes = state.setdefault("vetoes", {})

    p = portfolio.plan(a["scored"], a["rejected"], market, a["regime"], held, state["watchlist"], vetoes,
                       capital=risk["nav"] or risk.get("priced_nav") or 1.0, as_of=as_of, last_rebalance=state.get("last_rebalance"),
                       positions_meta=_positions_meta(state, last))
    if official:  # what-if runs never advance the cooling-off clock
        state["watchlist"] = p["watchlist"]

    from . import signals as sigs
    run_signals = {}
    for t, act in p["actions"].items():  # automated pre-trade checks (NSE / SEC) on every proposed buy
        if act["action"] == "BUY":
            res = checks.run(t)
            bad = [f"{k.replace('_', ' ')}: {v['detail']}" for k, v in res.items()
                   if v["status"] == "FAIL" or (v["status"] == "UNKNOWN" and k in checks.CRITICAL)]
            if t in a["metrics"].index:
                run_signals[t] = sigs.run(t, a["metrics"], a["as_of"])
                bad += [f"{k.replace('_', ' ')}: {v['detail']}" for k, v in run_signals[t].items() if v["status"] in ("FAIL", "WAIT")]
            if bad:
                act.update(action="HOLD" if t in held else "WATCH", reason="pre-trade check: " + "; ".join(bad),
                           target_weight=round(held.get(t, 0.0), 4))
        elif t in held and act["action"] in ("HOLD", "TRIM") and t in a["metrics"].index:
            run_signals[t] = sigs.run(t, a["metrics"], a["as_of"])
            severe = [v["detail"] for v in run_signals[t].values() if v["status"] == "FAIL"]
            if severe:  # a severe corporate event on a holding is a sell, like a failed safety gate
                act.update(action="SELL", reason="corporate red flag: " + severe[0], target_weight=0.0)

    final = {}
    for t, act in p["actions"].items():
        packet = review_packet(t, a) if act["action"] == "BUY" else {}
        reviews = load_reviews(market, a["as_of"], t) if packet else []
        try:
            c = review.combine(act["action"], reviews, packet, held=t in held, require_review_for_buy=require_review)
        except Exception as e:  # a malformed review must never abort the run (gate SELLs still need to execute)
            c = {"action": act["action"] if act["action"] != "BUY" else ("HOLD" if t in held else "WATCH"),
                 "review_reasons": [f"review processing error: {type(e).__name__}"], "human_review": True, "veto": None}
        if c.get("veto") and t not in vetoes and official:
            vetoes[t] = {"date": a["as_of"], "reason": c["veto"], "human_review": c["human_review"]}
            journal.record("veto", {"market": market, "ticker": t, **vetoes[t]})
        final[t] = {**act, "quant_action": act["action"], **c}
        if c["action"] != "BUY" and act["action"] == "BUY":
            final[t]["target_weight"] = round(held.get(t, 0.0), 4)

    if official:  # every official call joins the out-of-sample track record
        from . import scorecard
        for t in a["rejected"]:  # grading rejected stocks over time tests whether the gates avoid losers
            if t not in final:
                scorecard.log(market, t, a["as_of"], "AVOID", last.get(t), "run")

    executions = []
    if mode == "autopilot":
        executions = broker.execute(state, final, last, as_of, risk, adv=a["metrics"]["adv_value_60d"].dropna().to_dict(),
                                    today=today)
        if p["rebalance_due"] and any(e["status"] == "FILLED" for e in executions):
            state["last_rebalance"] = a["as_of"]
    if official:
        broker.save(state)

    # What the user sees is the SAME recommendation analyze_stock gives (stock_report). The paper book's
    # own decision (which additionally needs reviews in autopilot) is kept alongside as paper_action.
    shown = {}
    for t, f in final.items():
        if t not in a["metrics"].index or t not in a["gates"]:
            shown[t] = {**f, "paper_action": f["action"]}
            continue
        rec = stock_report(t, a, state, log=official)["recommendation"]
        shown[t] = {**f, "action": rec["action"], "start_date": rec["start_date"], "sell_date": rec.get("sell_date"),
                    "conviction": rec["conviction"], "position_size": rec["position_size"], "why": rec["why"],
                    "risks": rec["risks"][:3], "reason": rec["why"][0] if rec["why"] else f["reason"],
                    "paper_action": f["action"], "paper_reason": f["reason"]}

    result = {
        "as_of": a["as_of"], "mode": mode if official else f"{mode} (what-if: state untouched)", "market": market, "universe": universe,
        "regime": a["regime"], "risk": risk, "validation": latest_validation(market),
        "data_snapshot_sha": a["snapshot_sha"], "policy_sha": policy_sha(),
        "standing_vetoes": vetoes,
        "funnel": {"universe": len(a["metrics"]), "passed_gates": len(a["scored"]),
                   "rejected": len(a["rejected"]), "top_rejection_reasons": _top_reasons(a["rejected"])},
        "actions": shown, "executions": executions,
        "pending_reviews": sorted(t for t, f in final.items() if f.get("pending_review")),
        "not_covered": checks.NOT_COVERED[market],
        "top_ranked": _top_table(a["scored"], 15),
    }
    journal.record("run", {k: v for k, v in result.items() if k != "top_ranked"})
    return result


def policy_sha() -> str:
    """Hash of the frozen policy; a change invalidates earlier validation results."""
    return hashlib.sha256(Path(__file__).with_name("policy.py").read_bytes()).hexdigest()[:16]


def latest_validation(market: str) -> dict:
    """Most recent `stockfunnel validate` result for this market, so every report carries the evidence."""
    found = []
    for f in Path("reports").glob("validate-*.json"):
        try:
            v = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        if v.get("market") == market:
            found.append((f.stat().st_mtime, f, v))
    if not found:
        return {"status": "NOT_RUN", "note": f"run `stockfunnel validate` for a {market} universe"}
    mtime, f, v = max(found, key=lambda x: x[0])
    age = (date.today() - date.fromtimestamp(mtime)).days
    if v.get("policy_sha") != policy_sha():
        return {"status": "STALE", "note": "policy.py changed since the last validation; re-run `stockfunnel validate`"}
    return {"status": "PASSED" if v["passed"] else "FAILED", "universe": v["universe"], "period": v["period"],
            "age_days": age, "stale": age > 90, "checks": v["checks"],
            "strategy_sharpe": v["strategy"]["sharpe"], "equal_weight_sharpe": v["equal_weight_universe"]["sharpe"],
            "strategy_max_dd": v["strategy"]["max_drawdown"], "equal_weight_max_dd": v["equal_weight_universe"]["max_drawdown"]}


def _top_reasons(rejected: dict) -> dict:
    counts: dict[str, int] = {}
    for reasons in rejected.values():
        for r in reasons:
            counts[r] = counts.get(r, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1])[:8])


def _top_table(scored: pd.DataFrame, n: int) -> list[dict]:
    cols = ["rank", "name", "sector", "score"] + [c for c in scored.columns if c.startswith("theme_")]
    top = scored.head(n)[[c for c in cols if c in scored]]
    return [{"ticker": t, **{k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()}}
            for t, r in top.iterrows()]
