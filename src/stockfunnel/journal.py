"""Append-only decision journal with a hash chain, so past decisions can't be silently rewritten
and every recommendation can later be scored against what actually happened (calibration)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

JOURNAL = Path("journal/decisions.jsonl")


def _last_hash(path: Path) -> str:
    if not path.exists():
        return "genesis"
    lines = path.read_text().strip().splitlines()
    return json.loads(lines[-1])["hash"] if lines else "genesis"


def record(kind: str, payload: dict, path: Path = JOURNAL) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, "payload": payload, "prev": _last_hash(path)}
    entry["hash"] = hashlib.sha256(json.dumps(entry, sort_keys=True, default=str).encode()).hexdigest()
    with path.open("a") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")
    return entry


def verify(path: Path = JOURNAL) -> bool:
    prev = "genesis"
    for line in path.read_text().strip().splitlines() if path.exists() else []:
        e = json.loads(line)
        h = e.pop("hash")
        if e["prev"] != prev or hashlib.sha256(json.dumps(e, sort_keys=True, default=str).encode()).hexdigest() != h:
            return False
        prev = h
    return True


def entries(kind: str | None = None, path: Path = JOURNAL) -> list[dict]:
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().strip().splitlines()]
    return [r for r in rows if kind is None or r["kind"] == kind]
