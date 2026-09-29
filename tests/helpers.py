import asyncio

import httpx

from config import DEFAULTS, deep_merge
from db import DB
from net import Http


def run(coro):
    return asyncio.run(coro)


def cfg(**overrides):
    c = deep_merge(DEFAULTS, overrides)
    c["rate_limits"] = {h: 100_000 for h in c["rate_limits"]}
    return c


def http_with(handler, db: DB | None = None) -> Http:
    h = Http({}, transport=httpx.MockTransport(handler), base_backoff=0.001, default_per_minute=100_000,
             on_request=(lambda host: db.count_request(host)) if db else None)
    for host in ("api.dexscreener.com", "api.rugcheck.xyz", "api.gopluslabs.io", "discord.com", "api.telegram.org"):
        h._limits[host] = 100_000
    return h
