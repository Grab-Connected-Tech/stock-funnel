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

import numpy as np

from . import broker, data, funnel, regime

PAGE = Path(__file__).with_name("web") / "index.html"
_lock = threading.Lock()  # one funnel computation at a time (shared caches and state file)


def _json_default(o):
    if isinstance(o, np.generic):
        return o.item()
    return str(o)


def _clean(o):
    """NaN -> None so the browser gets valid JSON."""
    if isinstance(o, float) and o != o:
        return None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    return o


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


ROUTES = {
    "/api/analyze": lambda q: funnel.analyze_stock(q["ticker"].strip().upper(), q.get("universe") or None),
    "/api/regime": lambda q: regime.assess(q["market"]),
    "/api/validation": lambda q: funnel.latest_validation(q["market"]),
    "/api/status": lambda q: _status(q["market"]),
    "/api/run": lambda q: _run(q["universe"]),
}


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
        fn = ROUTES.get(url.path)
        if not fn:
            return self._send(404, b'{"error":"not found"}', "application/json")
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            with _lock:
                out = fn(q)
            self._send(200, json.dumps(_clean(out), default=_json_default).encode(), "application/json")
        except KeyError as e:
            self._send(400, json.dumps({"error": f"missing parameter {e}"}).encode(), "application/json")
        except Exception as e:  # surface the reason (e.g. tamper refusal) instead of a blank page
            self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), "application/json")


def main():
    port = int(os.environ.get("STOCKFUNNEL_PORT", "8765"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"stockfunnel UI → http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
