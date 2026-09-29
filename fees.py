"""Global fees paid (Solana only).

For tokens that already passed basic safety: total fees traders paid on the
token's swaps = base fee + priority fee (Helius `fee`, in lamports) + Jito tips
(native transfers to Jito tip accounts). Pulled from Helius (free tier) on the
pool address, cached, recalculated every few minutes at most.

Used ONLY as a filter/warning:
  - global fees < min_global_fees_sol      => "low activity", no alert
  - fees / volume below min_fees_to_volume => "likely fake/wash volume" warning
It never overrides DANGER and never counts as proof a coin is safe.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass

from db import DB
from net import Http
from safety import PASS, UNKNOWN, WARN
from sources.dex_source import MarketInfo
from wallets import helius_transactions

log = logging.getLogger(__name__)

LAMPORTS = 1_000_000_000
WSOL = "So11111111111111111111111111111111111111112"
DEFAULT_JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
]


@dataclass
class FeeResult:
    status: str                       # ok | unknown
    global_fees_sol: float | None = None
    fees_24h_sol: float | None = None
    fees_to_volume: float | None = None   # fees USD (24h) / volume USD (24h)
    sampled_txs: int = 0
    complete: bool = False            # True = every swap since launch was counted
    estimated: bool = False           # True = scaled up from a sample
    low_activity: bool = False
    wash_volume: bool = False
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def checks(self) -> list[tuple[str, str, str]]:
        if self.status != "ok" or self.global_fees_sol is None:
            return [("global_fees", UNKNOWN, self.detail or "not available")]
        approx = "~" if self.estimated else "" if self.complete else "≥"
        out = [("global_fees", WARN if self.low_activity else PASS,
                f"{approx}{self.global_fees_sol:.2f} SOL paid in fees" + (" - LOW ACTIVITY" if self.low_activity else ""))]
        if self.fees_to_volume is not None:
            out.append(("wash_volume", WARN if self.wash_volume else PASS,
                        f"fees/volume {self.fees_to_volume * 100:.3f}%" +
                        (" - likely fake/wash volume" if self.wash_volume else "")))
        return out


def sum_fees(txs: list[dict], jito: set[str]) -> float:
    """Fees in SOL: base + priority (tx.fee) + Jito tips."""
    lamports = 0
    for tx in txs:
        lamports += int(tx.get("fee") or 0)
        for nt in tx.get("nativeTransfers") or []:
            if nt.get("toUserAccount") in jito:
                lamports += int(nt.get("amount") or 0)
    return lamports / LAMPORTS


def evaluate(market: MarketInfo, txs: list[dict], complete: bool, sol_usd: float | None, cfg: dict,
             jito: set[str]) -> FeeResult:
    """Pure calculation (tested with normal / low-activity / wash-traded samples)."""
    now = time.time()
    txs = [t for t in txs if t.get("timestamp")]
    sampled = sum_fees(txs, jito)
    day = [t for t in txs if now - float(t["timestamp"]) <= 86400]
    fees_24h = sum_fees(day, jito)
    txns_24h = (market.buys_h24 or 0) + (market.sells_h24 or 0)
    estimated = False
    oldest = min((float(t["timestamp"]) for t in txs), default=now)
    covers_24h = complete or now - oldest >= 86400
    if not covers_24h and day and txns_24h > len(day):
        fees_24h *= txns_24h / len(day)  # scale sample up to DexScreener's 24h swap count
        estimated = True
    total = sampled if complete else max(sampled, fees_24h)
    if not complete and fees_24h > sampled:
        estimated = True

    ratio = None
    vol = market.volume_h24 or 0
    if sol_usd and vol > 0:
        ratio = fees_24h * sol_usd / vol

    res = FeeResult("ok", round(total, 4), round(fees_24h, 4), ratio, len(txs), complete, estimated)
    res.low_activity = total < float(cfg.get("min_global_fees_sol", 1.5))
    res.wash_volume = (ratio is not None and vol >= float(cfg.get("wash_min_volume_usd", 20000))
                       and ratio < float(cfg.get("min_fees_to_volume", 0.001)))
    return res


class GlobalFees:
    def __init__(self, db: DB, http: Http, cfg: dict, helius_key: str):
        self.db = db
        self.http = http
        self.cfg = cfg.get("fees") or {}
        self.key = helius_key
        self.jito = set(self.cfg.get("jito_tip_accounts") or DEFAULT_JITO_TIP_ACCOUNTS)
        self._sol_usd: tuple[float, float] | None = None  # (price, fetched_at)

    async def sol_price(self, market: MarketInfo) -> float | None:
        if (market.quote_symbol or "").upper() in ("SOL", "WSOL") and market.price_native and market.price_usd:
            return market.price_usd / market.price_native
        if self._sol_usd and time.time() - self._sol_usd[1] < 600:
            return self._sol_usd[0]
        r = await self.http.get_json(f"https://api.dexscreener.com/latest/dex/tokens/{WSOL}")
        if r.ok and isinstance(r.data, dict):
            for p in r.data.get("pairs") or []:
                if (p.get("baseToken") or {}).get("address") == WSOL and p.get("priceUsd"):
                    self._sol_usd = (float(p["priceUsd"]), time.time())
                    return self._sol_usd[0]
        return None

    async def check(self, market: MarketInfo) -> FeeResult:
        if market.chain != "solana" or not self.cfg.get("enabled", True):
            return FeeResult("unknown", detail="Solana only")
        if not self.key:
            return FeeResult("unknown", detail="needs HELIUS_API_KEY")
        if not market.pair_address:
            return FeeResult("unknown", detail="no pool address")
        max_age = float(self.cfg.get("recheck_minutes", 5)) * 60
        row = self.db.q1("""SELECT result_json FROM fee_checks WHERE chain = ? AND address = ? AND checked_at >= ?
                            ORDER BY checked_at DESC LIMIT 1""", (market.chain, market.address, time.time() - max_age))
        if row:
            return FeeResult(**json.loads(row["result_json"]))

        txs: list[dict] = []
        before = None
        complete = False
        for _ in range(int(self.cfg.get("max_pages", 5))):
            page = await helius_transactions(self.http, self.key, market.pair_address, "SWAP", 100, before)
            if page is None:
                if not txs:
                    return FeeResult("unknown", detail="Helius unavailable")
                break
            txs += page
            oldest = min((float(t.get("timestamp") or 0) for t in page), default=0)
            if len(page) < 100 or (market.pair_created_at and oldest and oldest <= market.pair_created_at):
                complete = True
                break
            before = page[-1].get("signature")
        res = evaluate(market, txs, complete, await self.sol_price(market), self.cfg, self.jito)
        self.db.x("INSERT INTO fee_checks (chain, address, checked_at, result_json) VALUES (?, ?, ?, ?)",
                  (market.chain, market.address, time.time(), json.dumps(res.to_dict())))
        return res
