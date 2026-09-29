"""Fomo app (fomo.family) trader wallets and coin theses.

Finding (checked while building): fomo.family publishes NO public API. The app
talks to a private, login-gated backend (prod-api.fomo.family, Privy auth) that
"can change or be restricted at any time". So by default this source runs in
fallback mode and uses fomo_wallets.yaml.

Supported out of the box: FOMO API (fomoapi.io, third-party) - free key, 250k
credits/month: leaderboard call = 250 credits, trader wallet lookup = 2,500. Put
FOMO_API_KEY in .env; endpoints are in config (fomo.leaderboard_url / user_url).
`python main.py fomo-test` shows exactly what the leaderboard returns. Parsing is generic: any Solana/EVM wallet
address found under wallet-ish keys is picked up; any text under thesis-ish
keys is collected. Polite: default every 6h.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from db import DB
from extract import chain_family, normalize
from net import Http

log = logging.getLogger(__name__)

WALLET_KEYS = ("wallet", "address", "solana", "evm", "pubkey", "publickey")
THESIS_KEYS = ("thesis", "theses", "note", "comment", "text", "body", "content")
NAME_KEYS = ("handle", "username", "name", "displayname")


def find_wallets(data: Any) -> list[dict]:
    """Walk arbitrary JSON; return [{wallet, chain, label}] from wallet-ish keys."""
    out: dict[str, dict] = {}

    def walk(node: Any, label: str | None) -> None:
        if isinstance(node, dict):
            lbl = label
            for k in NAME_KEYS:
                if isinstance(node.get(k), str):
                    lbl = node[k]
                    break
            for k, v in node.items():
                if isinstance(v, str) and any(w in k.lower() for w in WALLET_KEYS):
                    addr = normalize(v)
                    fam = chain_family(v)
                    if addr and fam:
                        out.setdefault(addr, {"wallet": addr, "chain": "solana" if fam == "solana" else "ethereum",
                                              "label": f"fomo:{lbl}" if lbl else "fomo"})
                else:
                    walk(v, lbl)
        elif isinstance(node, list):
            for v in node:
                walk(v, label)

    walk(data, None)
    return list(out.values())


def find_handles(data: Any) -> list[str]:
    """Trader handles in leaderboard order (first occurrence wins)."""
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k in NAME_KEYS:
                v = node.get(k)
                if isinstance(v, str) and v.strip() and v not in out:
                    out.append(v.strip().lstrip("@"))
                    break
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return out


def find_theses(data: Any) -> list[dict]:
    out = []

    def walk(node: Any, author: str | None) -> None:
        if isinstance(node, dict):
            a = next((node[k] for k in NAME_KEYS if isinstance(node.get(k), str)), author)
            for k, v in node.items():
                if isinstance(v, str) and k.lower() in THESIS_KEYS and len(v.strip()) >= 10:
                    out.append({"author": a, "text": v.strip()[:1000]})
                else:
                    walk(v, a)
        elif isinstance(node, list):
            for v in node:
                walk(v, author)

    walk(data, None)
    return out


class FomoSource:
    def __init__(self, http: Http, db: DB, cfg: dict, api_key: str = ""):
        self.http = http
        self.db = db
        self.cfg = cfg.get("fomo") or {}
        self.api_key = api_key
        self.wallets: list[dict] = []
        self.last_ok: float | None = None
        self.mode = "fallback"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "X-API-Key": self.api_key} if self.api_key else {}

    async def refresh_wallets(self) -> tuple[list[dict], str | None]:
        """Returns (wallets, error). Leaderboard traders' wallets come either straight from the
        leaderboard response or, when it only lists handles, from one profile lookup per NEW
        trader (lookups are expensive on the free FOMO API plan, so each trader is resolved once)."""
        url = self.cfg.get("leaderboard_url")
        if not url or not self.api_key:
            self.mode = "fallback"
            return [], None
        r = await self.http.get_json(url, headers=self._headers(), log_name="fomo-leaderboard")
        if not r.ok:
            self.mode = "fallback"
            return [], f"Fomo leaderboard unavailable ({r.error}); using fomo_wallets.yaml"
        top = int(self.cfg.get("max_traders", 15))
        wallets = find_wallets(r.data)[:top]
        user_tmpl = self.cfg.get("user_url")
        if not wallets and user_tmpl:
            resolved = 0
            for handle in find_handles(r.data)[:top]:
                known = self.db.q("SELECT wallet, chain, label FROM wallets WHERE source = 'fomo' AND label = ?",
                                  (f"fomo:{handle}",))
                if known:
                    wallets += [dict(k) for k in known]
                    continue
                if resolved >= int(self.cfg.get("resolve_per_run", 3)) or not self.db.mark_seen("fomo_user", handle):
                    continue
                resolved += 1
                u = await self.http.get_json(user_tmpl.format(handle=handle), headers=self._headers(),
                                             log_name="fomo-user")
                if u.ok:
                    for w in find_wallets(u.data):
                        w["label"] = f"fomo:{handle}"
                        wallets.append(w)
        if not wallets:
            self.mode = "fallback"
            return [], "Fomo leaderboard returned no wallets (format changed?); using fomo_wallets.yaml"
        self.wallets, self.mode, self.last_ok = wallets, "live", time.time()
        return wallets, None

    async def theses_for(self, address: str) -> list[dict]:
        tmpl = self.cfg.get("theses_url")
        if not tmpl:
            return []
        r = await self.http.get_json(tmpl.format(address=address), headers=self._headers(), log_name="fomo-theses")
        if not r.ok:
            return []
        theses = find_theses(r.data)
        for t in theses:
            self.db.x("INSERT OR IGNORE INTO fomo_theses (address, author, text, fetched_at) VALUES (?, ?, ?, ?)",
                      (address, t["author"], t["text"], time.time()))
        return theses
