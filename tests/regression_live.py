"""Live regression checks from the second external review (needs network; data-dependent).
    uv run python tests/regression_live.py
Only structural expectations: a verdict like "Lupin is WAIT" legitimately changes as new data arrives."""
from stockfunnel import funnel

failures = []


def expect(name, ok, detail=""):
    print(("ok  " if ok else "FAIL"), name, detail)
    if not ok:
        failures.append(name)


run = funnel.run("nifty200", "manual")
for t, a in list(run["actions"].items())[:10]:  # same stock, same data => same verdict in both tools
    rec = funnel.analyze_stock(t)["recommendation"]
    expect(f"run == analyze for {t}", (a["action"], a.get("start_date")) == (rec["action"], rec["start_date"]),
           f"{a['action']} / {rec['action']}")
lic = funnel.analyze_stock("LICI.NS")
expect("insurer: no FCF yield", lic["metrics"].get("fcf_yield") is None)
for t in ("HST", "SPG"):
    r = funnel.analyze_stock(t)
    expect(f"{t}: REIT gates (FFO, net debt/EBITDA)", "reit_leverage" in r["gates"]["checks"] and "leverage" not in r["gates"]["checks"])
    rev = r["recommendation"]["signals"]["analyst_revisions"]
    expect(f"{t}: missing revision data never reads as zero", "0 up, 0 down" not in rev["detail"], rev["detail"][:70])
aapl = funnel.analyze_stock("AAPL")["recommendation"]
expect("US buys say '(no proven edge)'", aapl["action"] not in ("BUY", "BUY AFTER") or "no proven edge" in aapl["action_label"])
print("\n" + ("all live checks passed" if not failures else f"{len(failures)} failed: {failures}"))
raise SystemExit(1 if failures else 0)
