"""Coins YOU are in, so exit warnings only go out for those.

You tell the bot from your phone, through the same ntfy app the alerts come in:
  * tap "I bought" on an alert (or "I sold" on an exit warning), or
  * type `in <contract address>` / `out <contract address>` into your ntfy topic
    (also accepted: bought / buy / sold / sell).

The bot never sees your wallet or trades - it only stores which coins you said you hold.
Messages are read from your private topic and the button topic `<topic>-holdings`;
anything that isn't one of those commands is ignored.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import httpx

from db import DB
from extract import extract

log = logging.getLogger(__name__)

COMMAND_RE = re.compile(r"^\s*(in|bought|buy|out|sold|sell)\b[\s:]*(.*)$", re.I | re.S)
IN_WORDS = {"in", "bought", "buy"}


def parse_command(text: str) -> tuple[str, str, str | None] | None:
    """'in <CA>' -> ("in", address, chain hint); 'sold <CA>' -> ("out", ...). None if not a command."""
    m = COMMAND_RE.match(text or "")
    if not m:
        return None
    rest = m.group(2).strip()
    chain = None
    prefix = re.match(r"([a-z]+):(\S+)", rest, re.I)  # "solana:<address>" from the buttons
    if prefix:
        chain, rest = prefix.group(1), prefix.group(2)
    cands = extract(rest)
    if not cands:
        return None
    action = "in" if m.group(1).lower() in IN_WORDS else "out"
    hint = chain.lower() if chain else cands[0].chain_hint
    return action, cands[0].address, hint


def holdings_topic(topic: str) -> str:
    return f"{topic}-holdings"


class Holdings:
    def __init__(self, db: DB, cfg: dict):
        self.db = db
        self.max_days = float((cfg.get("alerts") or {}).get("holding_max_days", 14))

    def add(self, chain: str, address: str) -> bool:
        """True if newly added (False if already held)."""
        return self.db.x("""INSERT INTO holdings (chain, address, opened_at) VALUES (?, ?, ?)
                            ON CONFLICT (chain, address) DO UPDATE SET opened_at = excluded.opened_at, closed_at = NULL
                            WHERE holdings.closed_at IS NOT NULL""", (chain, address, time.time())) == 1

    def remove(self, address: str) -> int:
        return self.db.x("UPDATE holdings SET closed_at = ? WHERE address = ? AND closed_at IS NULL",
                         (time.time(), address))

    def active(self) -> list:
        """Open holdings (anything older than holding_max_days is treated as forgotten)."""
        return self.db.q("SELECT chain, address, opened_at FROM holdings WHERE closed_at IS NULL AND opened_at >= ?",
                         (time.time() - self.max_days * 86400,))

    def holds(self, chain: str, address: str) -> bool:
        return any(r["chain"] == chain and r["address"] == address for r in self.active())


async def handle_message(pipe, holdings: Holdings, event: dict) -> str | None:
    """Apply one ntfy message. Returns what happened (for logs / tests), None if not a command."""
    if event.get("event") != "message" or not pipe.db.mark_seen("ntfy_cmd", str(event.get("id") or "")):
        return None
    cmd = parse_command(str(event.get("message") or ""))
    if not cmd:
        return None
    action, address, hint = cmd
    alerter = pipe.alerter
    if action == "out":
        n = holdings.remove(address)
        tok = pipe.db.q1("SELECT symbol FROM tokens WHERE address = ?", (address,))
        name = f"${tok['symbol']}" if tok and tok["symbol"] else address[:8] + "…"
        await alerter.send_report(f"Stopped watching {name}", "No more exit warnings for it." if n else
                                  "You weren't marked as in this coin.", record_kind="holding")
        return f"out {address}"
    tok = pipe.db.q1("SELECT chain, symbol FROM tokens WHERE address = ?", (address,))
    if not tok:
        # A coin the bot hasn't seen yet: look it up now so it can be watched.
        await pipe.process_text(f"{hint + ':' if hint and hint != 'evm' else ''}{address}", "holding",
                                f"ntfy:{event.get('id')}")
        tok = pipe.db.q1("SELECT chain, symbol FROM tokens WHERE address = ?", (address,))
    if not tok:
        await alerter.send_report("Couldn't find that coin", f"No trading pair found yet for {address[:8]}…. "
                                  "Send it again in a minute.", record_kind="holding")
        return f"unknown {address}"
    holdings.add(tok["chain"], address)
    name = f"${tok['symbol']}" if tok["symbol"] else address[:8] + "…"
    await alerter.send_report(f"Watching {name} for you",
                              "You'll get an EXIT WARNING if the dev sells, liquidity drops, smart wallets dump or "
                              "the chart breaks. Tap \"I sold\" on a warning (or send `out <address>`) when you're out.",
                              record_kind="holding")
    return f"in {address}"


async def listen(pipe, holdings: Holdings, server: str, topic: str, token: str = "") -> None:
    """Follow your ntfy topic + the button topic and apply in/out commands (reconnects forever)."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"{server.rstrip('/')}/{topic},{holdings_topic(topic)}/json"
    since, backoff = "12h", 5.0  # on (re)start, catch up on recent taps; message ids make it idempotent
    while True:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=90)) as client:
                async with client.stream("GET", url, params={"since": since}, headers=headers) as resp:
                    resp.raise_for_status()
                    backoff = 5.0
                    async for line in resp.aiter_lines():
                        try:
                            event = json.loads(line) if line.strip() else None
                        except ValueError:
                            continue
                        if not event:
                            continue
                        if event.get("id"):
                            since = event["id"]
                        try:
                            done = await handle_message(pipe, holdings, event)
                        except Exception:
                            log.exception("holdings: couldn't apply a message")
                            continue
                        if done:
                            log.info("holdings: %s", done)
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            log.warning("holdings listener: %s; reconnecting in %.0fs", type(exc).__name__, backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 600)
