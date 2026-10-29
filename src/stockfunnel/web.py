"""Local web UI: `stockfunnel-ui` then open http://127.0.0.1:8765.

A thin JSON layer over the existing functions (funnel, regime, broker). It adds no decision
logic and exposes nothing the CLI doesn't: no autopilot, no policy edits, no halt resets.
Binds to localhost only.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import broker, data, funnel, protocol, regime

PAGE = Path(__file__).with_name("web") / "index.html"
_lock = threading.Lock()  # one funnel computation at a time (shared caches and state file)


def _status(market: str) -> dict:
    st = broker.load(market)
    last, as_of = data.last_prices(data.prices(list(st["positions"]) or [data.MARKETS[market]["benchmark"]], "1mo")["close"])
    risk = broker.risk_status(st, last, as_of)  # computed on the loaded copy; never saved from the UI
    w = broker.weights(st, last)
    return {"market": market, "universe": st.get("universe"), "risk": risk, "cash": st["cash"],
            "positions": [{"ticker": t, "shares": p["shares"], "avg_cost": p["avg_cost"], "last": last.get(t),
                           "weight": w.get(t), "opened": p.get("opened")} for t, p in st["positions"].items()],
            "watchlist": st["watchlist"], "vetoes": st.get("vetoes", {}), "trades": st["trades"][-20:],
            "as_of": str(as_of)}


def _run(universe: str) -> dict:
    if universe not in data.UNIVERSES:
        raise ValueError(f"unknown universe; choose one of {sorted(data.UNIVERSES)}")
    return funnel.run(universe, "manual")  # manual only: the UI recommends, it never trades


def _track_record(_q):
    from . import scorecard
    return scorecard.grade()


def _plan(q):
    from .plan import RISK_MAX_LOSS, plan_investment
    amount, months = float(q["amount"]), int(q.get("months") or 12)
    risk = q.get("risk") or None
    if amount <= 0 or not 1 <= months <= 600 or q.get("market", "IN") not in ("IN", "US") or (risk and risk not in RISK_MAX_LOSS):
        raise ValueError("amount > 0, months 1-600, market IN/US, risk conservative/moderate/growth")
    target = float(q["target"]) if q.get("target") else None
    return plan_investment(amount, q.get("market", "IN"), months, target, risk=risk)


def _param(name, desc, required=True, enum=None):
    schema = {"type": "string", **({"enum": enum} if enum else {})}
    return {"name": name, "in": "query", "required": required, "description": desc, "schema": schema}


def openapi(port: int) -> dict:
    """OpenAPI 3.1 description so non-MCP agents (e.g. GPT actions) can discover the endpoints."""
    ok = {"200": {"description": "JSON result", "content": {"application/json": {"schema": {"type": "object"}}}}}
    get = lambda op, summary, params=(): {"get": {"operationId": op, "summary": summary,  # noqa: E731
                                                   "parameters": list(params), "responses": ok}}
    return {
        "openapi": "3.1.0",
        "info": {"title": "stockfunnel", "version": "0.1.0", "description": protocol.PROTOCOL},
        "servers": [{"url": f"http://127.0.0.1:{port}"}],
        "paths": {
            "/api/analyze": get("analyzeStock", "Final recommendation for one stock (action is final; never upgrade it)",
                                [_param("ticker", "e.g. AAPL or TCS.NS"),
                                 _param("universe", "peer universe", False, sorted(data.UNIVERSES))]),
            "/api/regime": get("marketRegime", "Market regime and allowed equity exposure", [_param("market", "", True, ["US", "IN"])]),
            "/api/run": get("runFunnel", "Screen an index (recommendations only)", [_param("universe", "", True, sorted(data.UNIVERSES))]),
            "/api/validation": get("evidence", "Backtest verdict for a market", [_param("market", "", True, ["US", "IN"])]),
            "/api/status": get("portfolioStatus", "Paper portfolio", [_param("market", "", True, ["US", "IN"])]),
            "/api/plan": get("planInvestment", "Turn an amount into a plan (or the index fund); odds are history, not forecasts",
                             [_param("amount", "money to invest (INR or USD)"), _param("market", "", False, ["US", "IN"]),
                              _param("months", "holding period in months", False),
                              _param("risk", "caps the stock share", False, ["conservative", "moderate", "growth"]),
                              _param("target", "hoped-for monthly return as a decimal, e.g. 0.01", False)]),
            "/api/track_record": get("trackRecord", "How past recommendations performed vs the market"),
            "/api/protocol": get("protocol", "Judgement protocol and field guide for LLMs"),
        },
    }


ROUTES = {
    "/api/analyze": lambda q: funnel.analyze_stock(q["ticker"].strip().upper(), q.get("universe") or None),
    "/api/regime": lambda q: regime.assess(q["market"]),
    "/api/validation": lambda q: funnel.latest_validation(q["market"]),
    "/api/status": lambda q: _status(q["market"]),
    "/api/run": lambda q: _run(q["universe"]),
    "/api/track_record": _track_record,
    "/api/plan": _plan,
    "/api/protocol": lambda q: {"protocol": protocol.PROTOCOL, "field_guide": protocol.FIELD_GUIDE},
}
PORT = int(os.environ.get("STOCKFUNNEL_PORT", "8765"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if url.path == "/openapi.json":
            return self._send(200, protocol.dumps(openapi(PORT)).encode(), "application/json")
        fn = ROUTES.get(url.path)
        if not fn:
            return self._send(404, b'{"error":"not found"}', "application/json")
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            with _lock:
                out = fn(q)
            self._send(200, protocol.dumps(out).encode(), "application/json")
        except KeyError as e:
            self._send(400, json.dumps({"error": f"missing parameter {e}"}).encode(), "application/json")
        except Exception as e:  # surface the reason (e.g. tamper refusal) instead of a blank page
            self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), "application/json")


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"stockfunnel UI → http://127.0.0.1:{PORT}  ·  API spec: /openapi.json  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
