import httpx

from db import DB
from extract import Candidate
from sources.dex_source import DexScreener, best_pair, parse_pair, profile_links
from tests.helpers import http_with, run


def pair(addr="TOKEN", chain="solana", liq=1000.0, **kw):
    p = {"chainId": chain, "pairAddress": f"P{liq}", "baseToken": {"address": addr, "name": "N", "symbol": "S"},
         "liquidity": {"usd": liq}, "pairCreatedAt": 1_700_000_000_000}
    p.update(kw)
    return p


def test_parse_pair_defensive():
    assert parse_pair({}) is None
    assert parse_pair("junk") is None
    m = parse_pair({"chainId": "solana", "baseToken": {"address": "x"}, "liquidity": None,
                    "priceUsd": "abc", "info": {"socials": [{"platform": "twitter", "handle": "h"}, "bad"]}}, "x")
    assert m.liquidity_usd is None and m.price_usd is None
    assert m.socials == [{"type": "twitter", "url": "h"}]
    assert parse_pair(pair()).pair_created_at == 1_700_000_000


def test_best_pair_filters_chain_and_base_token():
    pairs = [pair(liq=100), pair(liq=5000), pair(chain="tron", liq=99999), pair(addr="OTHER", liq=88888)]
    assert best_pair(pairs, "TOKEN", ["solana"]).liquidity_usd == 5000
    assert best_pair(pairs, "NOPE", ["solana"]) is None


def test_profile_links():
    g = profile_links({"links": [{"type": "twitter", "url": "https://x.com/a"}, {"url": "https://t.me/b"},
                                 {"label": "Website", "url": "https://c.io"}, {"type": "discord", "url": "d"}, "junk"]})
    assert g == {"x": ["https://x.com/a"], "telegram": ["https://t.me/b"], "website": ["https://c.io"], "other": ["d"]}


def test_poll_filters_chains_and_dedupes():
    profiles = [{"chainId": "solana", "tokenAddress": "6ce9TvjRyG4XEwjEcm16AXyf2hxrXtCEsth429Uk7MwU", "url": "u"},
                {"chainId": "tron", "tokenAddress": "TX"}, {"chainId": "solana", "tokenAddress": "notvalid"}]
    http = http_with(lambda req: httpx.Response(200, json=profiles))
    dex, db = DexScreener(http, ["solana"]), DB()
    first = run(dex.poll(db))
    assert len(first) == 1 and first[0][0].source == "dexscreener"
    assert run(dex.poll(db)) == []


def test_poll_failure_returns_none():
    http = http_with(lambda req: httpx.Response(500))
    assert run(DexScreener(http, ["solana"]).poll(DB())) is None


def test_resolve_dex_link_falls_back_to_pair_lookup():
    def handler(req):
        if "/latest/dex/tokens/" in req.url.path:
            return httpx.Response(200, json={"pairs": None})
        return httpx.Response(200, json={"pairs": [pair(addr="REALTOKEN", chain="ethereum", liq=7000)]})
    dex = DexScreener(http_with(handler), ["ethereum"])
    m, ok = run(dex.resolve(Candidate("0xpair", "ethereum", "dex")))
    assert ok and m.address == "REALTOKEN"


def test_resolve_no_pair_and_api_down():
    dex = DexScreener(http_with(lambda r: httpx.Response(200, json={"pairs": []})), ["solana"])
    assert run(dex.resolve(Candidate("A", "solana"))) == (None, True)
    dex = DexScreener(http_with(lambda r: httpx.Response(503)), ["solana"])
    assert run(dex.resolve(Candidate("A", "solana"))) == (None, False)
