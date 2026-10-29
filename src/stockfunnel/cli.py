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
           "| Ticker | Recommendation | Conviction | Size | Why | Paper book |", "|---|---|---|---|---|---|"]
    order = {a: i for i, a in enumerate(("BUY", "BUY AFTER", "SELL", "TRIM", "HOLD", "WAIT", "WATCH", "AVOID"))}
    for t, a in sorted(r["actions"].items(), key=lambda kv: (order.get(kv[1]["action"], 9), kv[0])):
        act = a.get("action_label", a["action"]) + (f" {a['start_date']}" if a.get("start_date") else "") + (f" (sell {a['sell_date']})" if a.get("sell_date") else "")
        flag = " ⚠ human review" if a.get("human_review") else ""
        size = f"{a['position_size']:.1%}" if a.get("position_size") else "–"
        out.append(f"| {t} | **{act}**{flag} | {a.get('conviction') or '–'} | {size} | {a['reason']} | {a.get('paper_action', '')} |")
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
    out += ["", f"_Every BUY above passed automated NSE/SEC checks. Not covered: {r['not_covered']}_"]
    out += ["", "## Top-30 basket (alphabetical: the order inside the basket carries no information)",
            "| Ticker | Name | Sector | Value | Quality | Mom | LowVol | Yield |", "|---|---|---|---|---|---|---|---|"]
    for x in r["top_ranked"]:
        th = [x.get(f"theme_{k}") for k in ("value", "quality", "momentum", "low_vol", "yield")]
        out.append(f"| {x['ticker']} | {x.get('name')} | {x.get('sector')} | "
                   + " | ".join("–" if v is None or v != v else f"{v:+.2f}" for v in th) + " |")
    out += ["", "_Not investment advice. Systematic screen on free, non-point-in-time data; see README limitations._"]
    return "\n".join(out)


def render_stock(r: dict) -> str:
    if r["verdict"] == "NO DATA":
        return f"{r['ticker']}: {r['detail']}"
    m, reg, b, rec = r["metrics"], r["regime"], r["your_book"], r["recommendation"]
    act = rec.get("action_label", rec["action"]) + (f" {rec['start_date']}" if rec["start_date"] else "")
    out = [f"# {r['ticker']} · {m.get('name', '')} · {m.get('sector', '')} · {r['as_of']}", "",
           f"## Recommendation: {act}" + (f" · conviction {rec['conviction']}" if rec["conviction"] else ""), ""]
    if rec["position_note"]:
        out.append(f"**Size:** {rec['position_note']}")
    out += ["**Why:**"] + [f"- {w}" for w in rec["why"]]
    if rec["risks"]:
        out += ["**Risks:**"] + [f"- {w}" for w in rec["risks"]]
    if rec["sell_if"]:
        out += ["**Sell if:**"] + [f"- {w}" for w in rec["sell_if"]]
    if rec.get("alternative"):
        out += [f"**Alternative:** {rec['alternative']}"]
    if rec.get("signals"):
        out += ["**Hidden signals:**"] + [f"- {dict(POSITIVE='+', FAIL='✕', WARN='!', WAIT='◷').get(v['status'], '·')} "
                                          f"{k.replace('_', ' ')}: {v['detail']}" for k, v in rec["signals"].items()]
    if rec["checks"]:
        out += ["**Automated checks:**"] + [f"- {dict(PASS='✓', FAIL='✕', WARN='!').get(v['status'], '?')} {k.replace('_', ' ')}: {v['detail']}"
                                           for k, v in rec["checks"].items()]
    if rec["conviction"]:
        out.append(f"Conviction tally ({rec['conviction_score']} pts): {', '.join(rec['conviction_points'])}")
    out.append(f"_Evidence scope: {rec['evidence_scope']}_")
    out += [f"Next review: {rec['next_review']}", "", "## Details", "",
           f"**Funnel stage: {r['verdict']}{' (BLOCKED)' if r.get('blocked') else ''}**: {r['detail']}",
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
    out += ["", f"_Data coverage: {r['not_covered']}_"]
    return "\n".join(out)


def _validation_line(v: dict) -> str:
    if v["status"] in ("NOT_RUN", "STALE"):
        return f"**Evidence**: ⚠ {v['status']}: {v['note']}"
    rp = v.get("full_funnel_replay", {})
    line = (f"**Evidence: {v['status']}** · price factors {v['universe']} {v['period']}: {v['price_factor_backtest']} "
            f"(Sharpe {v['strategy_sharpe']} vs equal-weight {v['equal_weight_sharpe']})")
    if rp.get("status") not in (None, "NOT_RUN"):
        line += (f" · full-funnel replay {rp['period']}: {rp['status']} (top 30 beat the market in "
                 f"{rp['top30_beat_market_share']:.0%} of windows)")
    if v["status"] != "PASSED":
        line += f" ⚠ {v['note']}; an index or factor ETF is an equally good default"
    return line + (" (stale: re-run validate)" if v.get("stale") else "")


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
    sub.add_parser("scorecard", help="grade past recommendations against the market (live track record)")
    p = sub.add_parser("replay", help="hindsight experiment: rewind N months, rank with the data of then, grade vs since "
                                      "(kept separate: never touches the journal, scorecard or paper book)")
    p.add_argument("universe")
    p.add_argument("--months", type=int, default=3, help="how far back (single replay)")
    p.add_argument("--multi", type=int, metavar="N", help="instead: replay every month-end over the last N months")
    p.add_argument("--benchmark", metavar="MONTHS", help="instead: framework vs ETFs over look-backs, e.g. 3,6,12,36")
    p.add_argument("--compare", metavar="TICKERS", help="with --benchmark: your own tickers to compare, e.g. AAPL,MSFT")
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
    elif a.cmd == "scorecard":
        from . import scorecard
        print(json.dumps(scorecard.grade(), indent=2))
    elif a.cmd == "replay":
        from . import replay
        if a.benchmark:
            out = replay.benchmark(a.universe, [int(x) for x in a.benchmark.split(",")],
                                   a.compare.split(",") if a.compare else None)
            name = f"{a.universe}-benchmark"
        else:
            out = replay.multi_date(a.universe, a.multi) if a.multi else replay.top_picks(a.universe, a.months)
            name = f"{a.universe}-{'multi' + str(a.multi) if a.multi else str(a.months) + 'm'}"
        path = replay.save(name, out)
        print(json.dumps(out | {"saved_to": str(path)}, indent=2, default=str))
    elif a.cmd == "verify-journal":
        ok = journal.verify()
        print("journal OK" if ok else "journal TAMPERED")
        return 0 if ok else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
