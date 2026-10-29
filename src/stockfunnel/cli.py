"""stockfunnel CLI. `stockfunnel --help`."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from . import broker, data, funnel, gates, journal, metrics, regime


def render(r: dict) -> str:
    reg, risk, f = r["regime"], r["risk"], r["funnel"]
    out = [f"# {r['universe']} · {r['as_of']} · mode: {r['mode']}", "",
           f"**Regime**: {reg['label']} · max equity {reg['max_equity_exposure']:.0%} · "
           f"momentum crash guard: {'ON' if reg['momentum_crash_guard'] else 'off'} · "
           f"index {reg['index_drawdown_from_1y_high']:.1%} from 1y high",
           f"**Portfolio**: NAV {risk['nav']:,.0f} · drawdown {risk['drawdown']:.1%} · "
           f"{'HALTED' if risk['halted'] else 'buys paused' if risk['buys_paused'] else 'normal'}",
           _validation_line(r["validation"]),
           f"**Funnel**: {f['universe']} → {f['passed_gates']} passed gates ({f['rejected']} rejected; top reasons: "
           + ", ".join(f"{k} {v}" for k, v in f["top_rejection_reasons"].items()) + ")", "",
           "| Ticker | Quant | Final | Target | Why |", "|---|---|---|---|---|"]
    order = {a: i for i, a in enumerate(("BUY", "TRIM", "SELL", "HOLD", "WATCH"))}
    for t, a in sorted(r["actions"].items(), key=lambda kv: order[kv[1]["action"]]):
        why = a["reason"] + (f" · review: {'; '.join(a['review_reasons'])}" if a.get("review_reasons") else "")
        flag = " ⚠ human review" if a.get("human_review") else ""
        out.append(f"| {t} | {a['quant_action']} | **{a['action']}**{flag} | {a['target_weight']:.1%} | {why} |")
    if r["standing_vetoes"]:
        out += ["", "## Standing vetoes (cleared only by a human: `stockfunnel clear-veto`)"] + [
            f"- {t}: {v['reason']} (since {v['date']})" for t, v in r["standing_vetoes"].items()]
    if r["executions"]:
        out += ["", "## Executions (paper)"] + [
            f"- {e['status']} {e['action']} {e['ticker']}" + (f" {e['shares']} @ {e['price']}" if e["status"] == "FILLED" else f": {e['reason']}")
            for e in r["executions"]]
    if r["pending_reviews"]:
        out += ["", f"## Pending reviews ({len(r['pending_reviews'])})",
                "BUYs need a valid review first: `stockfunnel packet <universe> <TICKER>` → reviewer fills the schema → "
                "`stockfunnel review <universe> <TICKER> review.json --reviewer <you>`.", ", ".join(r["pending_reviews"])]
    out += ["", "## Manual checks before real money"] + [f"- [ ] {c}" for c in r["manual_checks_required"]]
    out += ["", "## Top ranked", "| # | Ticker | Name | Sector | Score | Value | Quality | Mom | LowVol | Yield |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for x in r["top_ranked"]:
        th = [x.get(f"theme_{k}") for k in ("value", "quality", "momentum", "low_vol", "yield")]
        out.append(f"| {int(x['rank'])} | {x['ticker']} | {x.get('name')} | {x.get('sector')} | {x['score']:.2f} | "
                   + " | ".join("–" if v is None or v != v else f"{v:+.2f}" for v in th) + " |")
    out += ["", "_Not investment advice. Systematic screen on free, non-point-in-time data; see README limitations._"]
    return "\n".join(out)


def render_stock(r: dict) -> str:
    if r["verdict"] == "NO DATA":
        return f"{r['ticker']}: {r['detail']}"
    m, reg, b = r["metrics"], r["regime"], r["your_book"]
    out = [f"# {r['ticker']} · {m.get('name', '')} · {m.get('sector', '')} · {r['as_of']}", "",
           f"**Verdict: {r['verdict']}**: {r['detail']}",
           f"Peers: {r['peer_universe']}" + ("" if r["member"] else " (not a member; ranked alongside it)"),
           f"Regime: {reg['label']}, max equity {reg['max_equity_exposure']:.0%}", ""]
    if r["themes"]:
        out += ["| Theme | z-score vs sector peers |", "|---|---|"] + [f"| {k} | {v:+.2f} |" for k, v in r["themes"].items()]
    checks = r["gates"]["checks"]
    bad = ", ".join(f"{k} ({v})" for k, v in checks.items() if v != "PASS")
    passing = sum(v == "PASS" for v in checks.values())
    out += ["", f"Gates: {passing}/{len(checks)} pass" + (f"; not passing: {bad}" if bad else ""), ""]
    out += ["Key metrics: " + ", ".join(f"{k} {v:.3g}" if isinstance(v, float) else f"{k} {v}" for k, v in m.items()
                                       if k not in ("name", "sector"))]
    if b["held"] or b["watchlist_since"] or b["standing_veto"]:
        out.append(f"Your book: held={b['held']}, watchlist since {b['watchlist_since']}, veto={b['standing_veto']}")
    out += ["", "Before real money:"] + [f"- [ ] {c}" for c in r["manual_checks"]]
    return "\n".join(out)


def _validation_line(v: dict) -> str:
    if v["status"] in ("NOT_RUN", "STALE"):
        return f"**Evidence**: ⚠ {v['status']}: {v['note']}"
    line = (f"**Evidence** ({v['universe']}, {v['period']}): Sharpe {v['strategy_sharpe']} vs equal-weight "
            f"{v['equal_weight_sharpe']}, max DD {v['strategy_max_dd']:.0%} vs {v['equal_weight_max_dd']:.0%} → {v['status']}")
    if v["status"] == "FAILED":
        line += (" ⚠ no proven risk-adjusted edge over a passive equal-weight/index fund in this market; "
                 "treat output as risk control, and consider the ETF as the default")
    return line + (" (stale: re-run validate)" if v["stale"] else "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stockfunnel", description="Evidence-based stock selection funnel (US + India)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="run the full funnel")
    p.add_argument("universe", help="sp500 | nifty500 | nifty200 | nifty50 | path to ticker file")
    p.add_argument("--mode", choices=["manual", "autopilot"], default="manual")
    p.add_argument("--capital", type=float, help="starting paper capital (first run only)")
    p.add_argument("--limit", type=int, help="only the first N tickers (testing)")
    p.add_argument("--json", action="store_true")
    sub.add_parser("regime", help="market regime").add_argument("market", choices=["US", "IN"])
    p = sub.add_parser("analyze", help="full verdict for one stock, ranked against its peers")
    p.add_argument("ticker", help="e.g. AAPL, TCS.NS")
    p.add_argument("--universe", help="peer universe (default: sp500 for US, nifty200 for India)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("packet", help="review packet for an LLM/human reviewer")
    p.add_argument("universe"); p.add_argument("ticker")  # noqa: E702
    p = sub.add_parser("review", help="validate + store a review JSON")
    p.add_argument("universe"); p.add_argument("ticker"); p.add_argument("file")  # noqa: E702
    p.add_argument("--reviewer", required=True, help="who is reviewing (your initials, or the model name)")
    p = sub.add_parser("write-off", help="HUMAN ONLY: remove an unpriceable (delisted/renamed) position at zero")
    p.add_argument("market", choices=["US", "IN"]); p.add_argument("ticker")  # noqa: E702
    sub.add_parser("status", help="paper portfolio").add_argument("market", choices=["US", "IN"])
    sub.add_parser("reset-halt", help="HUMAN ONLY: clear the drawdown halt").add_argument("market", choices=["US", "IN"])
    p = sub.add_parser("clear-veto", help="HUMAN ONLY: clear a standing review veto")
    p.add_argument("market", choices=["US", "IN"]); p.add_argument("ticker")  # noqa: E702
    p = sub.add_parser("validate", help="price-factor backtest with deflated Sharpe")
    p.add_argument("universe")
    sub.add_parser("verify-journal", help="check the decision journal hash chain")
    a = ap.parse_args(argv)

    if a.cmd == "run":
        r = funnel.run(a.universe, a.mode, a.capital, a.limit)
        Path("reports").mkdir(exist_ok=True)
        stem = Path("reports") / f"{r['as_of']}-{Path(a.universe).stem}-{a.mode}"
        stem.with_suffix(".json").write_text(json.dumps(r, indent=2, default=str))
        stem.with_suffix(".md").write_text(render(r))
        print(json.dumps(r, indent=2, default=str) if a.json else render(r))
    elif a.cmd == "regime":
        print(json.dumps(regime.assess(a.market), indent=2))
    elif a.cmd == "analyze":
        r = funnel.analyze_stock(a.ticker, a.universe)
        print(json.dumps(r, indent=2, default=str) if a.json else render_stock(r))
    elif a.cmd == "packet":
        an = funnel.analyze_universe(a.universe)
        print(json.dumps(funnel.review_packet(a.ticker, an), indent=2, default=str))
    elif a.cmd == "review":
        try:
            rev = json.loads(Path(a.file).read_text())
        except json.JSONDecodeError as e:
            rev = f"invalid JSON: {e}"
        print(json.dumps(funnel.record_review(a.universe, a.ticker, rev, reviewer_id=f"cli:{a.reviewer}"), indent=2))
    elif a.cmd == "status":
        st = broker.load(a.market)
        last, as_of = data.last_prices(data.prices(list(st["positions"]) or [data.MARKETS[a.market]["benchmark"]], "1mo")["close"])
        print(json.dumps({"risk": broker.risk_status(st, last, as_of), "cash": st["cash"], "universe": st.get("universe"),
                          "standing_vetoes": st.get("vetoes", {}),
                          "weights": {k: round(v, 4) for k, v in broker.weights(st, last).items()},
                          "watchlist": st["watchlist"], "recent_trades": st["trades"][-10:]}, indent=2))
    elif a.cmd in ("reset-halt", "clear-veto", "write-off") and broker.tampered(broker.load(a.market)):
        if input("State does not match the journal (edited outside stockfunnel?). Type ACCEPT to adopt it: ").strip() != "ACCEPT":
            return 1
        journal.record("accept_tampered_state", {"market": a.market, "date": str(date.today())})
        broker.save(broker.load(a.market))  # re-anchor the hash to the accepted state
        return main(argv)  # re-dispatch now that the human has accepted the current state
    elif a.cmd == "write-off":
        if input(f"Type WRITE-OFF {a.ticker} to remove it at zero value: ").strip() != f"WRITE-OFF {a.ticker}":
            return 1
        st = broker.load(a.market)
        removed = st["positions"].pop(a.ticker, None)
        broker.save(st)
        journal.record("write_off", {"market": a.market, "ticker": a.ticker, "position": removed})
    elif a.cmd == "reset-halt":
        if input(f"Type RESET {a.market} to clear the halt: ").strip() != f"RESET {a.market}":
            return 1
        st = broker.load(a.market)
        last, _ = data.last_prices(data.prices(list(st["positions"]) or [data.MARKETS[a.market]["benchmark"]], "1mo")["close"])
        current = broker.nav(st, last)
        if current is None:
            print("cannot price every position; not resetting")
            return 1
        st["halted"], st["peak_nav"] = False, current
        broker.save(st)
        journal.record("reset_halt", {"market": a.market, "date": str(date.today()), "nav": current})
    elif a.cmd == "clear-veto":
        if input(f"Type CLEAR {a.ticker} to remove its standing veto: ").strip() != f"CLEAR {a.ticker}":
            return 1
        st = broker.load(a.market)
        removed = st.get("vetoes", {}).pop(a.ticker, None)
        broker.save(st)
        journal.record("clear_veto", {"market": a.market, "ticker": a.ticker, "removed": removed})
    elif a.cmd == "validate":
        from . import validate
        v = validate.run(a.universe) | {"policy_sha": funnel.policy_sha()}
        Path("reports").mkdir(exist_ok=True)
        (Path("reports") / f"validate-{Path(a.universe).stem}.json").write_text(json.dumps(v, indent=2))
        journal.record("validation", v)
        print(json.dumps(v, indent=2))
    elif a.cmd == "verify-journal":
        ok = journal.verify()
        print("journal OK" if ok else "journal TAMPERED")
        return 0 if ok else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
