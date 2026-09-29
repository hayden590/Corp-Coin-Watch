"""DexScreener free API: new token-profile feed + token/pair market data.

Endpoints used (public, no key):
  GET /token-profiles/latest/v1           ~60 req/min
  GET /latest/dex/tokens/{addresses}      ~300 req/min
  GET /latest/dex/pairs/{chain}/{pair}    ~300 req/min
The rate limiter in net.py keeps us under these (see rate_limits in config.yaml).
Parsers are defensive: unknown/missing fields become None rather than errors.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from db import DB, Sighting
from extract import Candidate, normalize
from net import Http

log = logging.getLogger(__name__)

BASE = "https://api.dexscreener.com"
SOURCE = "dexscreener"


@dataclass
class MarketInfo:
    chain: str
    address: str
    name: str | None = None
    symbol: str | None = None
    pair_address: str | None = None
    dex_id: str | None = None
    url: str | None = None
    price_usd: float | None = None
    liquidity_usd: float | None = None
    fdv: float | None = None
    market_cap: float | None = None
    volume_h24: float | None = None
    volume_h1: float | None = None
    volume_m5: float | None = None
    buys_m5: int | None = None
    sells_m5: int | None = None
    buys_h1: int | None = None
    sells_h1: int | None = None
    buys_h24: int | None = None
    sells_h24: int | None = None
    price_native: float | None = None
    quote_symbol: str | None = None
    price_change_m5: float | None = None
    price_change_h1: float | None = None
    price_change_h6: float | None = None
    price_change_h24: float | None = None
    pair_created_at: float | None = None  # epoch seconds
    websites: list[str] = field(default_factory=list)
    socials: list[dict[str, str]] = field(default_factory=list)

    @property
    def age_minutes(self) -> float | None:
        if self.pair_created_at is None:
            return None
        return max(0.0, (time.time() - self.pair_created_at) / 60)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["age_minutes"] = self.age_minutes
        return d


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None


def _socials(info: dict) -> list[dict[str, str]]:
    out = []
    for s in info.get("socials") or []:
        if not isinstance(s, dict):
            continue
        kind = (s.get("type") or s.get("platform") or "").lower()
        url = s.get("url") or s.get("handle") or ""
        if kind and url:
            out.append({"type": kind, "url": url})
    return out


def parse_pair(pair: dict, token_address: str | None = None) -> MarketInfo | None:
    if not isinstance(pair, dict):
        return None
    base = pair.get("baseToken") or {}
    chain = (pair.get("chainId") or "").lower()
    raw = base.get("address") or ""
    addr = normalize(raw) or raw or token_address
    if not chain or not addr:
        return None
    info = pair.get("info") or {}
    txns = pair.get("txns") or {}
    txns_m5 = txns.get("m5") or {}
    txns_h1 = txns.get("h1") or {}
    txns_h24 = txns.get("h24") or {}
    vol = pair.get("volume") or {}
    pc = pair.get("priceChange") or {}
    created = _f(pair.get("pairCreatedAt"))
    return MarketInfo(
        chain=chain,
        address=addr,
        name=base.get("name"),
        symbol=base.get("symbol"),
        pair_address=pair.get("pairAddress"),
        dex_id=pair.get("dexId"),
        url=pair.get("url"),
        price_usd=_f(pair.get("priceUsd")),
        liquidity_usd=_f((pair.get("liquidity") or {}).get("usd")),
        fdv=_f(pair.get("fdv")),
        market_cap=_f(pair.get("marketCap")),
        volume_h24=_f(vol.get("h24")),
        volume_h1=_f(vol.get("h1")),
        volume_m5=_f(vol.get("m5")),
        buys_m5=_i(txns_m5.get("buys")),
        sells_m5=_i(txns_m5.get("sells")),
        buys_h1=_i(txns_h1.get("buys")),
        sells_h1=_i(txns_h1.get("sells")),
        buys_h24=_i(txns_h24.get("buys")),
        sells_h24=_i(txns_h24.get("sells")),
        price_native=_f(pair.get("priceNative")),
        quote_symbol=(pair.get("quoteToken") or {}).get("symbol"),
        price_change_m5=_f(pc.get("m5")),
        price_change_h1=_f(pc.get("h1")),
        price_change_h6=_f(pc.get("h6")),
        price_change_h24=_f(pc.get("h24")),
        pair_created_at=created / 1000 if created and created > 1e11 else created,
        websites=[w.get("url") for w in info.get("websites") or [] if isinstance(w, dict) and w.get("url")],
        socials=_socials(info),
    )


def best_pair(pairs: list[dict], address: str, chains: list[str]) -> MarketInfo | None:
    """Highest-liquidity pair on a watched chain where `address` is the base token."""
    best: MarketInfo | None = None
    for p in pairs or []:
        m = parse_pair(p)
        if not m or m.chain not in chains or m.address.lower() != address.lower():
            continue
        if best is None or (m.liquidity_usd or 0) > (best.liquidity_usd or 0):
            best = m
    return best


class DexScreener:
    def __init__(self, http: Http, chains: list[str]):
        self.http = http
        self.chains = chains

    async def latest_profiles(self) -> list[dict] | None:
        r = await self.http.get_json(f"{BASE}/token-profiles/latest/v1")
        if not r.ok:
            return None
        data = r.data
        if isinstance(data, dict):  # tolerate a wrapped response
            data = data.get("data") or data.get("profiles") or []
        return [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []

    async def token_pairs(self, address: str) -> list[dict] | None:
        r = await self.http.get_json(f"{BASE}/latest/dex/tokens/{address}")
        if not r.ok:
            return None
        if not isinstance(r.data, dict):
            return []
        return [p for p in r.data.get("pairs") or [] if isinstance(p, dict)]

    async def pair(self, chain: str, pair_address: str) -> list[dict] | None:
        r = await self.http.get_json(f"{BASE}/latest/dex/pairs/{chain}/{pair_address}")
        if not r.ok:
            return None
        d = r.data if isinstance(r.data, dict) else {}
        pairs = d.get("pairs") or ([d["pair"]] if d.get("pair") else [])
        return [p for p in pairs if isinstance(p, dict)]

    async def resolve(self, cand: Candidate) -> tuple[MarketInfo | None, bool]:
        """Find the token's best pair. Returns (market, api_ok).

        For DexScreener links the path address may be a pair or a token, so try
        it as a token first, then as a pair.
        """
        pairs = await self.token_pairs(cand.address)
        if pairs is None:
            return None, False
        market = best_pair(pairs, cand.address, self.chains)
        if market or cand.kind != "dex" or not cand.chain_hint:
            return market, True
        pair_list = await self.pair(cand.chain_hint, cand.address)
        if pair_list is None:
            return None, False
        m = parse_pair(pair_list[0]) if pair_list else None
        if m and m.chain in self.chains:
            return m, True
        return None, True

    async def poll(self, db: DB) -> list[tuple[Sighting, dict]] | None:
        """New token profiles on watched chains. None if the API call failed."""
        profiles = await self.latest_profiles()
        if profiles is None:
            return None
        out = []
        for p in profiles:
            chain = (p.get("chainId") or "").lower()
            addr = normalize(p.get("tokenAddress") or "")
            if chain not in self.chains or not addr:
                continue
            if not db.mark_seen(SOURCE, f"{chain}:{addr}"):
                continue
            links = profile_links(p)
            s = Sighting(
                address=addr,
                chain_hint=chain,
                source=SOURCE,
                source_ref=p.get("url") or f"{chain}:{addr}",
                url=p.get("url"),
                text=(p.get("description") or "")[:500] or None,
            )
            out.append((s, links))
        return out


def profile_links(profile: dict) -> dict[str, list[str]]:
    """Group a profile's links into x / telegram / website / other."""
    groups: dict[str, list[str]] = {"x": [], "telegram": [], "website": [], "other": []}
    for link in profile.get("links") or []:
        if not isinstance(link, dict) or not link.get("url"):
            continue
        url = link["url"]
        kind = (link.get("type") or link.get("label") or "").lower()
        low = url.lower()
        if kind in ("twitter", "x") or "twitter.com/" in low or "x.com/" in low:
            groups["x"].append(url)
        elif kind == "telegram" or "t.me/" in low:
            groups["telegram"].append(url)
        elif kind == "website" or not kind:
            groups["website"].append(url)
        else:
            groups["other"].append(url)
    return groups
