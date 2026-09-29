"""Smart wallets: manual (smart_wallets.yaml), Fomo traders, and signal accounts'
known wallets.

Activity is fetched per *wallet* on a slow schedule and cached in SQLite, so
checking a CA is a local DB query (keeps us inside free API limits):
  Solana: Helius enhanced transactions API (free tier, HELIUS_API_KEY)
  EVM:    Blockscout (Ethereum/Base, no key) or Etherscan v2 (any chain, ETHERSCAN_API_KEY)

Every wallet gets a track record. A wallet only affects scoring once it has
`min_history` resolved trades - a leaderboard rank alone is never trusted.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from db import DB
from net import Http

log = logging.getLogger(__name__)

HELIUS_TX_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"
BLOCKSCOUT = {"ethereum": "https://eth.blockscout.com/api", "base": "https://base.blockscout.com/api"}
ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"
EVM_CHAIN_IDS = {"ethereum": 1, "bsc": 56, "base": 8453, "arbitrum": 42161, "polygon": 137}
QUOTE_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KLNBFCy3kR7gSMXomzpt",
}


@dataclass
class WalletRecord:
    wallet: str
    chain: str
    label: str | None
    source: str
    trades: int
    resolved: int
    win_rate: float | None
    rug_rate: float | None
    median_earliness_min: float | None
    multiplier: float  # 0 until enough history

    @property
    def trusted(self) -> bool:
        return self.multiplier > 0


async def helius_transactions(http: Http, api_key: str, address: str, tx_type: str | None = "SWAP",
                              limit: int = 100, before: str | None = None) -> list[dict] | None:
    params = {"api-key": api_key, "limit": limit}
    if tx_type:
        params["type"] = tx_type
    if before:
        params["before"] = before
    r = await http.get_json(HELIUS_TX_URL.format(address=address), params=params)
    if not r.ok or not isinstance(r.data, list):
        return None
    return [t for t in r.data if isinstance(t, dict)]


def parse_helius_swaps(wallet: str, txs: list[dict]) -> list[dict]:
    out = []
    for tx in txs:
        sig, ts = tx.get("signature"), tx.get("timestamp")
        if not sig or not ts:
            continue
        for tt in tx.get("tokenTransfers") or []:
            mint = tt.get("mint")
            if not mint or mint in QUOTE_MINTS:
                continue
            if tt.get("toUserAccount") == wallet:
                side = "buy"
            elif tt.get("fromUserAccount") == wallet:
                side = "sell"
            else:
                continue
            out.append({"token": mint, "side": side, "amount": _f(tt.get("tokenAmount")), "at": float(ts), "tx": sig})
    return out


def parse_evm_tokentx(wallet: str, rows: list[dict]) -> list[dict]:
    w = wallet.lower()
    out = []
    for r in rows:
        token = (r.get("contractAddress") or "").lower()
        if not token or not r.get("hash") or not r.get("timeStamp"):
            continue
        side = "buy" if (r.get("to") or "").lower() == w else "sell" if (r.get("from") or "").lower() == w else None
        if not side:
            continue
        try:
            amount = int(r.get("value") or 0) / 10 ** int(r.get("tokenDecimal") or 18)
        except (TypeError, ValueError):
            amount = None
        out.append({"token": token, "side": side, "amount": amount, "at": float(r["timeStamp"]), "tx": r["hash"]})
    return out


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Wallets:
    def __init__(self, db: DB, http: Http, cfg: dict, helius_key: str = "", etherscan_key: str = ""):
        self.db = db
        self.http = http
        self.cfg = cfg.get("wallets") or {}
        self.backtest_cfg = cfg.get("backtest") or {}
        self.helius_key = helius_key
        self.etherscan_key = etherscan_key
        self._warned: set[str] = set()

    # --- registry -------------------------------------------------------------
    def sync(self, cfg: dict, fomo_wallets: list[dict] | None = None) -> int:
        entries = []
        for w in cfg.get("smart_wallets") or []:
            entries.append((w.get("wallet"), w.get("chain"), w.get("label"), "manual", None))
        for w in (cfg.get("fomo_wallets") or []) + (fomo_wallets or []):
            entries.append((w.get("wallet"), w.get("chain", "solana"), w.get("label"), "fomo", None))
        for s in cfg.get("signals") or []:
            for w in s.get("wallets") or []:
                entries.append((w.get("address") or w.get("wallet"), w.get("chain"),
                                f"@{s.get('handle')}", "signal", str(s.get("x_user_id"))))
        n = 0
        for wallet, chain, label, source, owner in entries:
            if not wallet or not chain:
                continue
            wallet = wallet.lower() if wallet.startswith("0x") else wallet
            n += self.db.x(
                """INSERT INTO wallets (wallet, chain, label, source, owner_user_id) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(wallet, chain) DO UPDATE SET label = COALESCE(excluded.label, wallets.label),
                   owner_user_id = COALESCE(excluded.owner_user_id, wallets.owner_user_id)""",
                (wallet, chain.lower(), label, source, owner))
        return n

    # --- fetching -------------------------------------------------------------
    async def fetch_activity(self, wallet: str, chain: str) -> list[dict] | None:
        if chain == "solana":
            if not self.helius_key:
                self._warn_once("solana", "Solana wallet tracking needs HELIUS_API_KEY (free tier) - skipped")
                return None
            txs = await helius_transactions(self.http, self.helius_key, wallet, "SWAP", 50)
            return None if txs is None else parse_helius_swaps(wallet, txs)
        params = {"module": "account", "action": "tokentx", "address": wallet, "sort": "desc",
                  "page": 1, "offset": 50}
        if self.etherscan_key and chain in EVM_CHAIN_IDS:
            r = await self.http.get_json(ETHERSCAN_V2, params={**params, "chainid": EVM_CHAIN_IDS[chain],
                                                                "apikey": self.etherscan_key})
        elif chain in BLOCKSCOUT:
            r = await self.http.get_json(BLOCKSCOUT[chain], params=params)
        else:
            self._warn_once(chain, f"{chain} wallet tracking needs ETHERSCAN_API_KEY - skipped")
            return None
        if not r.ok or not isinstance(r.data, dict):
            return None
        rows = r.data.get("result")
        return parse_evm_tokentx(wallet, rows if isinstance(rows, list) else [])

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(msg)

    def store(self, wallet: str, chain: str, activity: list[dict]) -> int:
        n = 0
        for a in activity:
            n += self.db.x(
                """INSERT OR IGNORE INTO wallet_activity (wallet, chain, token, side, amount, at, tx)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (wallet, chain, a["token"], a["side"], a["amount"], a["at"], a["tx"]))
        self.db.x("UPDATE wallets SET last_polled = ? WHERE wallet = ? AND chain = ?", (time.time(), wallet, chain))
        return n

    async def poll_due(self, limit: int = 10) -> int:
        """Poll the stalest wallets (spread over time to stay within free limits)."""
        stale = time.time() - float(self.cfg.get("poll_minutes", 15)) * 60
        rows = self.db.q("""SELECT wallet, chain FROM wallets WHERE last_polled IS NULL OR last_polled < ?
                            ORDER BY COALESCE(last_polled, 0) LIMIT ?""", (stale, limit))
        total = 0
        for r in rows:
            act = await self.fetch_activity(r["wallet"], r["chain"])
            if act is not None:
                total += self.store(r["wallet"], r["chain"], act)
        return total

    async def poll_wallet(self, wallet: str, chain: str) -> None:
        act = await self.fetch_activity(wallet, chain)
        if act is not None:
            self.store(wallet, chain, act)

    # --- track records ------------------------------------------------------
    def record(self, wallet: str, chain: str) -> WalletRecord:
        meta = self.db.q1("SELECT label, source FROM wallets WHERE wallet = ? AND chain = ?", (wallet, chain))
        buys = self.db.q("""SELECT token, MIN(at) AS first_buy FROM wallet_activity
                            WHERE wallet = ? AND chain = ? AND side = 'buy' GROUP BY token""", (wallet, chain))
        win_gain = float(self.cfg.get("win_gain_pct", self.backtest_cfg.get("take_profit_pct", 50))) / 100
        resolved = wins = rugs = 0
        early = []
        for b in buys:
            outs = {o["horizon"]: o for o in self.db.q("SELECT * FROM outcomes WHERE address = ?", (b["token"],))}
            ref = outs.get("24h")
            if ref:
                resolved += 1
                if ref["rugged"]:
                    rugs += 1
                elif (ref["max_gain"] or 0) >= win_gain:
                    wins += 1
            first = self.db.first_sighting(b["token"])
            if first:
                early.append((b["first_buy"] - first["seen_at"]) / 60)
        win_rate = wins / resolved if resolved else None
        rug_rate = rugs / resolved if resolved else None
        mult = 0.0
        if resolved >= int(self.cfg.get("min_history", 10)):
            base = float(self.cfg.get("baseline_win_rate", 0.3))
            mult = max(0.25, min(2.0, (win_rate or 0) / base)) * (1 - min(0.8, rug_rate or 0))
        early.sort()
        return WalletRecord(wallet, chain, meta["label"] if meta else None, meta["source"] if meta else "?",
                            len(buys), resolved, win_rate, rug_rate,
                            early[len(early) // 2] if early else None, round(mult, 3))

    # --- per-token queries --------------------------------------------------
    def smart_buys(self, token: str) -> list[dict]:
        token = token.lower() if token.startswith("0x") else token
        rows = self.db.q("""SELECT a.wallet, a.chain, MIN(CASE WHEN a.side='buy' THEN a.at END) AS bought_at,
                                   MAX(CASE WHEN a.side='sell' THEN a.at END) AS last_sell,
                                   SUM(CASE WHEN a.side='buy' THEN 1 ELSE 0 END) AS n_buys
                            FROM wallet_activity a JOIN wallets w ON w.wallet = a.wallet AND w.chain = a.chain
                            WHERE a.token = ? GROUP BY a.wallet, a.chain""", (token,))
        out = []
        for r in rows:
            if not r["n_buys"]:
                continue
            rec = self.record(r["wallet"], r["chain"])
            out.append({"wallet": r["wallet"], "label": rec.label, "source": rec.source,
                        "bought_at": r["bought_at"], "sold": r["last_sell"] is not None and r["last_sell"] > r["bought_at"],
                        "record": rec})
        return out

    def exits_since(self, token: str, since: float) -> list[dict]:
        return [dict(r) for r in self.db.q(
            """SELECT a.wallet, w.label, a.at FROM wallet_activity a
               JOIN wallets w ON w.wallet = a.wallet AND w.chain = a.chain
               WHERE a.token = ? AND a.side = 'sell' AND a.at >= ?""", (token, since))]

    def dump_flags(self, token: str) -> list[str]:
        """'Possible dump on followers': a signal account's own wallet sells the CA
        within `dump_window_minutes` after that account posted it."""
        window = float(self.cfg.get("dump_window_minutes", 60)) * 60
        flags = []
        rows = self.db.q(
            """SELECT w.wallet, w.label, w.owner_user_id, a.at AS sold_at, s.seen_at AS posted_at
               FROM wallets w
               JOIN wallet_activity a ON a.wallet = w.wallet AND a.chain = w.chain AND a.side = 'sell' AND a.token = ?
               JOIN sightings s ON s.author_id = w.owner_user_id AND s.address = ? AND s.source = 'x'
               WHERE w.owner_user_id IS NOT NULL AND a.at >= s.seen_at AND a.at <= s.seen_at + ?""",
            (token, token, window))
        for r in rows:
            mins = (r["sold_at"] - r["posted_at"]) / 60
            flags.append(f"{r['label'] or r['wallet'][:8]} wallet SOLD {mins:.0f} min after posting it")
        return sorted(set(flags))

    def signal_wallets(self, user_id: str) -> list[tuple[str, str]]:
        return [(r["wallet"], r["chain"]) for r in
                self.db.q("SELECT wallet, chain FROM wallets WHERE owner_user_id = ?", (user_id,))]
