"""Snapshot every tracked CA at its first full assessment (all features, scores,
verdict, price), then record outcomes at 15m / 1h / 6h / 24h after first sighting:
max gain, max drawdown, final return, rugged y/n (liquidity -80% or honeypot).
The 24h outcome keeps the price path so strategies can be replayed exactly.
"""
from __future__ import annotations

import json
import logging
import time

from db import DB

log = logging.getLogger(__name__)

HORIZONS = {"15m": 900, "1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600}
RUG_LIQUIDITY_DROP = 0.8


def snapshot(db: DB, a, first_seen: float) -> bool:
    """Store the first-sighting snapshot (once per token) and schedule outcome jobs."""
    price = getattr(a.market, "price_usd", None)
    inserted = db.x(
        """INSERT OR IGNORE INTO feature_snapshots (chain, address, taken_at, first_seen_at, entry_price, verdict,
                                                     features_json) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (a.chain, a.address, time.time(), first_seen, price, a.verdict.label if a.verdict else None,
         json.dumps(a.features(), default=str))) == 1
    if inserted:
        for h, secs in HORIZONS.items():
            db.schedule("outcome", f"{a.chain}:{a.address}:{h}", first_seen + secs, a.chain, a.address, {"horizon": h})
    return inserted


def compute(entry: float, candles: list, start: float, end: float) -> dict | None:
    """candles: (ts, o, h, l, c, v) oldest first."""
    window = [c for c in candles if start - 900 < c[0] <= end]
    if not window or not entry:
        return None
    hi = max(c[2] for c in window)
    lo = min(c[3] for c in window)
    return {"max_gain": hi / entry - 1, "max_drawdown": lo / entry - 1, "final_return": window[-1][4] / entry - 1,
            "path": [[c[0], c[2], c[3], c[4]] for c in window]}


async def record_outcome(db: DB, row, dex, charts) -> str:
    """Run one due outcome job. Returns a short status for the pending_checks row."""
    h = json.loads(row["payload"] or "{}").get("horizon")
    snap = db.q1("SELECT * FROM feature_snapshots WHERE chain = ? AND address = ?", (row["chain"], row["address"]))
    if not snap or not h:
        return "no snapshot"
    entry = snap["entry_price"]
    start = snap["first_seen_at"]
    end = start + HORIZONS[h]
    tok = db.q1("SELECT pair_address FROM tokens WHERE chain = ? AND address = ?", (row["chain"], row["address"]))
    pairs = await dex.token_pairs(row["address"])
    from sources.dex_source import best_pair

    market = best_pair(pairs or [], row["address"], [row["chain"]]) if pairs is not None else None
    res = None
    path = await charts.path_since(row["chain"], tok["pair_address"] if tok else None, row["address"], start) if charts else None
    if path:
        res = compute(entry, path, start, end)
    if res is None:
        # Fallback: our own market snapshots + current price.
        prices = [r["price_usd"] for r in db.q("""SELECT price_usd FROM market_snapshots WHERE chain = ? AND address = ?
                                                   AND taken_at BETWEEN ? AND ? AND price_usd IS NOT NULL""",
                                                (row["chain"], row["address"], start, end))]
        if market and market.price_usd:
            prices.append(market.price_usd)
        if not prices or not entry:
            return "no price data"
        res = {"max_gain": max(prices) / entry - 1, "max_drawdown": min(prices) / entry - 1,
               "final_return": prices[-1] / entry - 1, "path": None}
    first_liq = json.loads(snap["features_json"]).get("liquidity_usd")
    rugged = False
    if market is not None and first_liq and (market.liquidity_usd or 0) < first_liq * (1 - RUG_LIQUIDITY_DROP):
        rugged = True
    if pairs is not None and market is None and first_liq:
        rugged = True  # pool gone entirely
    hp = db.q1("""SELECT report_json FROM safety_checks WHERE chain = ? AND address = ? ORDER BY checked_at DESC LIMIT 1""",
               (row["chain"], row["address"]))
    if hp and any(c.get("name") == "honeypot" and c.get("status") == "fail"
                  for c in json.loads(hp["report_json"]).get("checks") or []):
        rugged = True
    db.x("""INSERT OR REPLACE INTO outcomes (chain, address, horizon, recorded_at, max_gain, max_drawdown, final_return,
            rugged, path_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
         (row["chain"], row["address"], h, time.time(), res["max_gain"], res["max_drawdown"], res["final_return"],
          int(rugged), json.dumps(res["path"]) if h == "24h" and res["path"] else None))
    return f"{h}: gain {res['max_gain']:+.0%} dd {res['max_drawdown']:+.0%}{' RUGGED' if rugged else ''}"
