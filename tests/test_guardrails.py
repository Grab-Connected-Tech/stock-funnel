"""Self-check for the logic that must never break. Offline, no frameworks:
    uv run python tests/test_guardrails.py
"""
import itertools
import os
import random
import tempfile
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from stockfunnel import broker, journal, portfolio, review, validate
from stockfunnel.policy import ACTIONS

PACKET = {"ticker": "X", "facts": {"m.roe": 0.2, "m.vol_1y": 0.3, "m.name": "X Ltd", "g.piotroski": "PASS", "s.rank": 3}}
GOOD = {
    "ticker": "X", "reviewer_id": "model-a", "verdict": "PROCEED", "veto_flags": [], "confidence": "MEDIUM",
    "checklist": {k: "YES" for k in review.CHECKLIST},
    "premortem": [{"cause": c, "severity": "LOW"} for c in "abc"],
    "kill_criteria": [{"metric_id": "m.roe", "direction": "FALLS_BELOW"}, {"metric_id": "m.vol_1y", "direction": "RISES_ABOVE"}],
    "evidence": [{"fact_id": "g.piotroski", "supports": "BULL"}, {"fact_id": "m.roe", "supports": "BULL"},
                 {"fact_id": "m.vol_1y", "supports": "BEAR"}],
}
GOOD2 = dict(GOOD, reviewer_id="model-b")


def test_review_never_upgrades():
    assert review.validate(GOOD, PACKET) == []
    assert review.combine("BUY", [GOOD, GOOD2], PACKET, held=False)["action"] == "BUY"
    assert review.combine("BUY", [GOOD], PACKET, held=False)["action"] == "WATCH"           # one reviewer isn't enough
    assert review.combine("BUY", [GOOD, dict(GOOD)], PACKET, held=False)["action"] == "WATCH"  # same reviewer twice
    no_bear = dict(GOOD2, evidence=[e | {"supports": "BULL"} for e in GOOD["evidence"]])
    assert review.combine("BUY", [GOOD, no_bear], PACKET, held=False)["action"] == "WATCH"
    vanity = dict(GOOD2, kill_criteria=[{"metric_id": "m.name", "direction": "FALLS_BELOW"}] * 2)
    assert review.validate(vanity, PACKET)  # kill criteria must be decision metrics
    assert review.combine("BUY", [GOOD, dict(GOOD2, verdict="REJECT")], PACKET, held=False)["veto"]
    assert review.validate(dict(GOOD, reviewer_id=["x"]), PACKET)  # unhashable id is invalid, not a crash
    review.combine("BUY", [GOOD, dict(GOOD, reviewer_id=["x"])], PACKET, held=False)
    gate_only = dict(GOOD2, evidence=[{"fact_id": "g.piotroski", "supports": "BEAR"}, {"fact_id": "s.rank", "supports": "BULL"},
                                      {"fact_id": "m.roe", "supports": "BULL"}])
    assert review.validate(gate_only, PACKET)  # evidence needs >= 2 company metrics
    rnd = random.Random(0)
    for _ in range(3000):  # fuzz: random (often malformed) reviews
        r = dict(GOOD)
        r["verdict"] = rnd.choice(list(review.VERDICTS) + ["BUY", "STRONG_BUY", None])
        r["veto_flags"] = rnd.sample(review.VETO_FLAGS, rnd.randint(0, 2))
        r["checklist"] = {k: rnd.choice(review.TRI) for k in review.CHECKLIST}
        if rnd.random() < 0.2:
            r["evidence"] = [{"fact_id": "m.made_up", "supports": "BULL"}]
        if rnd.random() < 0.1:
            r["size_multiplier"] = 3
        for quant, held in itertools.product(ACTIONS, (True, False)):
            final = review.combine(quant, [r, GOOD, GOOD2], PACKET, held=held)["action"]
            assert ACTIONS.index(final) <= ACTIONS.index(quant), (quant, final, r)
            if quant != "BUY":
                assert final == quant  # reviews never cause or change a non-buy trade
    bad = dict(GOOD, evidence=[{"fact_id": "m.invented", "supports": "BULL"}])
    assert review.combine("BUY", [bad], PACKET, held=False)["action"] == "WATCH"
    assert review.combine("BUY", [], PACKET, held=False)["action"] == "WATCH"  # no review => no buy
    assert review.combine("BUY", [GOOD, GOOD2, dict(GOOD, verdict="REJECT")], PACKET, held=False)["action"] == "WATCH"


def test_capped_weights():
    rnd = np.random.default_rng(1)
    for _ in range(200):
        n = rnd.integers(5, 40)
        raw = pd.Series(rnd.uniform(0.1, 5, n), index=[f"T{i}" for i in range(n)])
        sectors = pd.Series(rnd.choice(list("ABCD"), n), index=raw.index)
        w = portfolio.capped_weights(raw, sectors, cap=0.07, sector_cap=0.25, total=0.5)
        assert w.max() <= 0.07 + 1e-9 and w.sum() <= 0.5 + 1e-9
        assert w.groupby(sectors).sum().max() <= 0.25 + 1e-9


def test_risk_off_halves_not_liquidates():
    idx = [f"T{i}" for i in range(30)]
    scored = pd.DataFrame({"rank": range(1, 31), "vol_1y": np.linspace(0.15, 0.45, 30), "sector": list("ABCDEF") * 5,
                           "adv_value_60d": 1e12, "ret_1m": 0.0, "score": 1.0}, index=idx)
    held = {t: 1 / 30 for t in idx}
    kw = dict(rejected={}, market="IN", holdings=held, watchlist={}, vetoes={}, capital=1e7,
              as_of=date(2026, 10, 28), last_rebalance="2026-09-01")
    off = portfolio.plan(scored, regime={"max_equity_exposure": 0.5}, **kw)["actions"]
    assert not any(a["action"] == "SELL" for a in off.values())
    assert abs(sum(a["target_weight"] for a in off.values()) - 0.5) < 0.06


def test_data_outage_holds_instead_of_selling():
    scored = pd.DataFrame({"rank": [1.0], "vol_1y": [0.2], "sector": ["A"], "adv_value_60d": [1e12],
                           "ret_1m": [0.0], "score": [1.0]}, index=["NEW"])
    acts = portfolio.plan(scored, {"OLD": ["min_market_cap?", "piotroski?"], "BAD": ["beneish_m"]}, "US",
                          {"max_equity_exposure": 1.0}, {"OLD": 0.05, "BAD": 0.05}, {}, {}, 1e6,
                          date(2026, 10, 28), last_rebalance=None)["actions"]
    assert acts["OLD"]["action"] == "HOLD" and acts["BAD"]["action"] == "SELL"
    empty = scored.iloc[0:0]
    portfolio.plan(empty, {}, "US", {"max_equity_exposure": 1.0}, {}, {}, {}, 1e6, date(2026, 10, 28))  # no crash


def test_broker_kill_switches_and_idempotency():
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        st = broker.load("US", 100_000)
        day = date(2026, 10, 28)
        buy = {"AAA": {"action": "BUY", "target_weight": 0.05, "reason": "t"}}
        status = broker.risk_status(st, {}, day)
        r1 = broker.execute(st, buy, {"AAA": 100.0}, day, status, adv={"AAA": 1e9}, today=day)
        assert r1[0]["status"] == "FILLED" and st["cash"] > 0
        r2 = broker.execute(st, buy, {"AAA": 100.0}, day, status, adv={"AAA": 1e9}, today=day)
        assert r2[0]["status"] == "REFUSED" and "duplicate" in r2[0]["reason"]
        assert broker.nav(st, {}) is None  # unpriced position => unknown, never "no loss"
        assert broker.risk_status(st, {}, day)["buys_paused"]
        stale = broker.execute(st, {"B": {"action": "BUY", "target_weight": 0.05}}, {"B": 10.0}, date(2026, 10, 1),
                               broker.risk_status(st, {"AAA": 100.0}, date(2026, 10, 1)), adv={"B": 1e9}, today=day)
        assert "stale" in stale[0]["reason"]
        # daily-loss pause: previous trading day NAV recorded, today's price -80% on the holding
        st2 = broker.load("US", 100_000)
        st2["positions"] = {"AAA": {"shares": 500, "avg_cost": 100.0}}
        st2["cash"] = 50_000
        broker.risk_status(st2, {"AAA": 100.0}, date(2026, 10, 27))
        s = broker.risk_status(st2, {"AAA": 92.0}, day)
        assert s["day_return"] < -0.03 and s["buys_paused"] and not s["halted"]
        assert broker.risk_status(st2, {"AAA": 92.0}, day)["buys_paused"]  # re-running same day can't reset it
        sell = broker.execute(st2, {"AAA": {"action": "SELL"}}, {"AAA": 92.0}, day, s, adv={}, today=day)
        assert sell[0]["status"] == "FILLED"  # risk-reducing orders are never blocked by pauses
        st2["peak_nav"] = 200_000
        assert broker.risk_status(st2, {}, day)["halted"]
        broker.save(st2)
        assert not broker.tampered(st2)
        st2["halted"] = False  # an agent editing state behind the code's back
        assert broker.tampered(st2)
        assert journal.verify()
        lines = Path("journal/decisions.jsonl").read_text().splitlines()
        Path("journal/decisions.jsonl").write_text("\n".join([lines[0].replace("AAA", "ZZZ")] + lines[1:]) + "\n")
        assert not journal.verify()  # tampering is detected

    with tempfile.TemporaryDirectory() as d:  # deleting state must not reset the book
        os.chdir(d)
        st = broker.load("IN", 1e6)
        broker.save(st)
        Path("state/IN.json").unlink()
        assert broker.tampered(broker.load("IN"))
        # one unpriceable holding: buys blocked, sells of the rest still execute
        st = broker.load("US", 100_000)
        st["positions"] = {"GONE": {"shares": 10, "avg_cost": 50.0}, "OK": {"shares": 100, "avg_cost": 10.0}}
        day = date(2026, 10, 28)
        status = broker.risk_status(st, {"OK": 10.0}, day)
        assert status["nav"] is None and status["buys_paused"]
        res = broker.execute(st, {"OK": {"action": "SELL"}, "NEW": {"action": "BUY", "target_weight": 0.05}},
                             {"OK": 10.0, "NEW": 5.0}, day, status, adv={"NEW": 1e9}, today=day)
        assert {r["ticker"]: r["status"] for r in res} == {"OK": "FILLED", "NEW": "REFUSED"}


def test_bad_prints_repaired():
    from stockfunnel import data
    px = pd.DataFrame({"A": [100, 101, 5050, 102, 103.0], "B": [10, 10, 2, 2, 2.0]})  # spike, unadjusted 1:5 split
    fixed, suspect = data.clean(px)
    assert suspect["A"].sum() == 2 and suspect["B"].sum() == 1
    assert fixed["A"].max() < 110 and abs(fixed["B"].iloc[-1] - 10) < 1e-9
    gap = pd.DataFrame({"C": [10, np.nan, 2, 2.0]})  # unadjusted split across a missing day
    assert data.clean(gap)[1]["C"].sum() == 1
    idx = pd.to_datetime(["2026-10-20", "2026-10-27", "2026-10-28"])
    last, as_of = data.last_prices(pd.DataFrame({"LIVE": [1, 2, 3.0], "SUSP": [5, np.nan, np.nan]}, index=idx))
    assert "SUSP" not in last and last["LIVE"] == 3.0  # suspended stock: no fictional price


def test_deflated_sharpe_penalizes_many_trials():
    rnd = np.random.default_rng(2)
    r = pd.Series(rnd.normal(0.005, 0.04, 120))
    few = validate.deflated_sharpe(r, [0.1, 0.12])
    many = validate.deflated_sharpe(r, list(rnd.normal(0.1, 0.08, 200)))
    assert many < few


if __name__ == "__main__":
    cwd = os.getcwd()
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            os.chdir(cwd)
            print("ok", name)
