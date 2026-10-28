"""stockfunnel CLI. `stockfunnel --help`."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from . import broker, data, funnel, gates, journal, metrics, regime, review


def render(r: dict) -> str:
    reg, risk, f = r["regime"], r["risk"], r["funnel"]
    out = [f"# {r['universe']} · {r['as_of']} · mode: {r['mode']}", "",
           f"**Regime**: {reg['label']} · max equity {reg['max_equity_exposure']:.0%} · "
           f"momentum crash guard: {'ON' if reg['momentum_crash_guard'] else 'off'} · "
           f"index {reg['index_drawdown_from_1y_high']:.1%} from 1y high",
           f"**Portfolio**: NAV {risk['nav']:,.0f} · drawdown {risk['drawdown']:.1%} · "
           f"{'HALTED' if risk['halted'] else 'buys paused' if risk['buys_paused'] else 'normal'}",
           f"**Funnel**: {f['universe']} → {f['passed_gates']} passed gates ({f['rejected']} rejected; top reasons: "
           + ", ".join(f"{k} {v}" for k, v in f["top_rejection_reasons"].items()) + ")", "",
           "| Ticker | Quant | Final | Target | Why |", "|---|---|---|---|---|"]
    order = {a: i for i, a in enumerate(("BUY", "TRIM", "SELL", "HOLD", "WATCH"))}
    for t, a in sorted(r["actions"].items(), key=lambda kv: order[kv[1]["action"]]):
        why = a["reason"] + (f" · review: {'; '.join(a['review_reasons'])}" if a.get("review_reasons") else "")
        flag = " ⚠ human review" if a.get("human_review") else ""
        out.append(f"| {t} | {a['quant_action']} | **{a['action']}**{flag} | {a['target_weight']:.1%} | {why} |")
    if r["executions"]:
        out += ["", "## Executions (paper)"] + [
            f"- {e['status']} {e['action']} {e['ticker']}" + (f" {e['shares']} @ {e['price']}" if e["status"] == "FILLED" else f": {e['reason']}")
            for e in r["executions"]]
    if r["pending_reviews"]:
        out += ["", f"## Pending reviews ({len(r['pending_reviews'])})",
                "BUYs need a valid review first: `stockfunnel packet <universe> <TICKER>` → reviewer fills the schema → "
                "`stockfunnel review <universe> <TICKER> review.json`.", ", ".join(r["pending_reviews"])]
    out += ["", "## Manual checks before real money"] + [f"- [ ] {c}" for c in r["manual_checks_required"]]
    out += ["", "## Top ranked", "| # | Ticker | Name | Sector | Score | Value | Quality | Mom | LowVol | Yield |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for x in r["top_ranked"]:
        th = [x.get(f"theme_{k}") for k in ("value", "quality", "momentum", "low_vol", "yield")]
        out.append(f"| {int(x['rank'])} | {x['ticker']} | {x.get('name')} | {x.get('sector')} | {x['score']:.2f} | "
                   + " | ".join("–" if v is None or v != v else f"{v:+.2f}" for v in th) + " |")
    out += ["", "_Not investment advice. Systematic screen on free, non-point-in-time data; see README limitations._"]
    return "\n".join(out)


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
    p = sub.add_parser("analyze", help="metrics + gates for one ticker")
    p.add_argument("ticker")
    p = sub.add_parser("packet", help="review packet for an LLM/human reviewer")
    p.add_argument("universe"); p.add_argument("ticker")  # noqa: E702
    p = sub.add_parser("review", help="validate + store a review JSON")
    p.add_argument("universe"); p.add_argument("ticker"); p.add_argument("file")  # noqa: E702
    sub.add_parser("status", help="paper portfolio").add_argument("market", choices=["US", "IN"])
    sub.add_parser("reset-halt", help="HUMAN ONLY: clear the drawdown halt").add_argument("market", choices=["US", "IN"])
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
        m = data.market_of(a.ticker)
        px = data.prices([a.ticker], "2y")
        mm = metrics.price_metrics(px["close"][a.ticker], px["volume"][a.ticker]) | metrics.fundamental_metrics(data.fundamentals(a.ticker))
        print(json.dumps({"metrics": mm, "gates": gates.evaluate(mm, m), "manual_checks": gates.MANUAL_CHECKS[m]}, indent=2, default=str))
    elif a.cmd == "packet":
        an = funnel.analyze_universe(a.universe)
        print(json.dumps(funnel.review_packet(a.ticker, an), indent=2, default=str))
    elif a.cmd == "review":
        an = funnel.analyze_universe(a.universe)
        rev = json.loads(Path(a.file).read_text())
        errs = review.validate(rev, funnel.review_packet(a.ticker, an))
        funnel.save_review(an["market"], an["as_of"], a.ticker, rev)
        journal.record("review", {"ticker": a.ticker, "as_of": an["as_of"], "valid": not errs, "review": rev})
        print(json.dumps({"stored": True, "valid": not errs, "errors": errs,
                          "note": "invalid reviews are stored and BLOCK the buy"}, indent=2))
    elif a.cmd == "status":
        st = broker.load(a.market)
        last = data.prices(list(st["positions"]) or [data.MARKETS[a.market]["benchmark"]], "1mo")["close"].iloc[-1].to_dict()
        print(json.dumps({"risk": broker.risk_status(st, last), "cash": st["cash"],
                          "weights": {k: round(v, 4) for k, v in broker.weights(st, last).items()},
                          "watchlist": st["watchlist"], "recent_trades": st["trades"][-10:]}, indent=2))
    elif a.cmd == "reset-halt":
        if input(f"Type RESET {a.market} to clear the halt: ").strip() != f"RESET {a.market}":
            return 1
        st = broker.load(a.market)
        st["halted"] = False
        st["peak_nav"] = broker.nav(st, {})
        broker.save(st)
        journal.record("reset_halt", {"market": a.market, "date": str(date.today())})
    elif a.cmd == "validate":
        from . import validate
        print(json.dumps(validate.run(a.universe), indent=2))
    elif a.cmd == "verify-journal":
        ok = journal.verify()
        print("journal OK" if ok else "journal TAMPERED")
        return 0 if ok else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
