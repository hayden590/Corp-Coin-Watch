"""Runs every source and background job as its own supervised loop, so one
failing source (X locked, Telegram logged out, an API down) never stops the rest.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Awaitable, Callable

from backtest import outcomes
from pipeline import Pipeline
from sources.fomo_source import FomoSource
from sources.telegram_source import TelegramSource
from sources.x_source import XBudgetExceeded, XSource, XUnavailable

log = logging.getLogger(__name__)


def jittered(seconds: float, pct: float) -> float:
    return max(1.0, seconds * random.uniform(1 - pct, 1 + pct))


class Monitor:
    def __init__(self, pipe: Pipeline, cfg: dict, x_source: XSource | None = None,
                 telegram: TelegramSource | None = None, fomo: FomoSource | None = None):
        self.pipe = pipe
        self.db = pipe.db
        self.cfg = cfg
        self.poll = cfg.get("poll") or {}
        self.jitter = float(self.poll.get("jitter_pct", 20)) / 100
        self.x_source = x_source
        self.telegram = telegram
        self.fomo = fomo
        self._fomo_alerted = False

    async def supervise(self, name: str, step: Callable[[], Awaitable[None]], interval: Callable[[], float]) -> None:
        backoff = 30.0
        while True:
            try:
                await step()
                backoff = 30.0
                await asyncio.sleep(interval())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s loop crashed; retrying in %.0fs", name, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 1800)

    # --- sources ------------------------------------------------------------
    async def dex_step(self) -> None:
        results = await self.pipe.poll_dex()
        if results:
            log.info("dex: %d new tokens, %d alerted", len(results), sum(r.status == "alerted" for r in results))

    async def x_step(self, kind: str) -> None:
        try:
            if kind == "sweep":
                tweets = await self.x_source.sweep()
            else:
                tweets = await self.x_source.watch(self.pipe.discovery.watched_ids())
        except XBudgetExceeded as exc:
            log.info("x %s skipped: %s", kind, exc)
            return
        except XUnavailable as exc:
            # All accounts locked/down: ONE alert (source_failed dedupes), everything else keeps running.
            await self.pipe.source_failed("x", str(exc), limit=1)
            await asyncio.sleep(float((self.cfg.get("x") or {}).get("no_account_cooldown_minutes", 15)) * 60)
            return
        await self.pipe.source_ok("x")
        for t in tweets:
            await self.pipe.handle_tweet(t)

    async def telegram_run(self) -> None:
        backoff = 30.0
        while True:
            try:
                await self.telegram.run(self.pipe.handle_telegram)
                await self.pipe.source_failed("telegram", "disconnected", limit=3)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("telegram: %s", exc)
                await self.pipe.source_failed("telegram", str(exc)[:200], limit=3)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 1800)

    async def fomo_step(self) -> None:
        wallets, err = await self.fomo.refresh_wallets()
        if err:
            log.warning("fomo: %s", err)
            if not self._fomo_alerted:
                self._fomo_alerted = True
                await self.pipe.alerter.send_system(err)
        elif wallets:
            self.pipe.wallets.sync(self.cfg, wallets)
            await self.pipe.source_ok("fomo")
        # theses for coins we recently alerted on
        if self.fomo.cfg.get("theses_url"):
            for t in self.db.q("SELECT address FROM tokens WHERE alerted_at >= ?", (time.time() - 86400,)):
                await self.fomo.theses_for(t["address"])

    # --- background jobs ------------------------------------------------------
    async def checks_step(self) -> None:
        for row in self.db.due_checks():
            kind = row["kind"]
            if kind == "tweet_persistence":
                if not self.pipe.x:
                    self.db.complete_check(row["id"], "skipped: no X")
                    continue
                res = await self.pipe.verifier.run_persistence_check(row, self.pipe.x)
                if res == "error":
                    self.db.x("UPDATE pending_checks SET due_at = ? WHERE id = ?", (time.time() + 600, row["id"]))
                    continue
                self.db.complete_check(row["id"], res)
                if res == "deleted":
                    log.warning("source tweet %s for %s was DELETED", row["ref"], row["address"])
                await self._reassess_address(row["address"])
            elif kind == "dump_check":
                wallet = json.loads(row["payload"] or "{}").get("wallet")
                if wallet:
                    await self.pipe.wallets.poll_wallet(wallet, row["chain"] or "solana")
                self.db.complete_check(row["id"], "polled")
                if self.pipe.wallets.dump_flags(row["address"]):
                    await self._reassess_address(row["address"])
            elif kind == "outcome":
                res = await outcomes.record_outcome(self.db, row, self.pipe.dex, self.pipe.charts)
                self.db.complete_check(row["id"], res)
            else:
                self.db.complete_check(row["id"], "unknown kind")

    async def _reassess_address(self, address: str) -> None:
        for t in self.db.q("SELECT chain FROM tokens WHERE address = ?", (address,)):
            await self.pipe.reassess(t["chain"], address)

    async def follow_up_step(self) -> None:
        hours = float((self.cfg.get("alerts") or {}).get("monitor_hours", 48))
        rows = self.db.q("SELECT chain, address, alerted_at FROM tokens WHERE alerted_at >= ?",
                         (time.time() - hours * 3600,))
        for r in rows:
            await self.pipe.follow_up(r["chain"], r["address"], r["alerted_at"])
        await self.pipe.paper.update(self.pipe.dex)

    async def slow_step(self) -> None:
        """Wallet polling, one social-graph refresh, discovery rules, account snapshots."""
        await self.pipe.wallets.poll_due(limit=int((self.cfg.get("wallets") or {}).get("poll_batch", 10)))
        if self.pipe.x:
            try:
                await self.pipe.graph.refresh_one()
                snap_s = float((self.cfg.get("x") or {}).get("snapshot_hours", 24)) * 3600
                for uid in self.pipe.discovery.watched_ids()[:10]:
                    last = self.db.q1("SELECT MAX(taken_at) AS t FROM account_snapshots WHERE user_id = ?", (uid,))["t"]
                    if not last or time.time() - last >= snap_s:
                        u = await self.pipe.x.user_by_id(uid)
                        if u:
                            self.pipe.discovery.observe_user(u)
            except XUnavailable as exc:
                log.info("slow step X work skipped: %s", exc)
        for change in self.pipe.discovery.apply_rules():
            await self.pipe.alerter.send_system(f"discovery: {change}")

    # --- run ------------------------------------------------------------------
    async def run(self) -> None:
        p, j = self.poll, self.jitter
        tasks = [
            self.supervise("dex", self.dex_step, lambda: jittered(p.get("dexscreener_seconds", 90), j)),
            self.supervise("checks", self.checks_step, lambda: 30),
            self.supervise("follow-up", self.follow_up_step, lambda: jittered(p.get("monitor_seconds", 300), j)),
            self.supervise("slow", self.slow_step, lambda: jittered(p.get("slow_minutes", 20) * 60, j)),
        ]
        if self.x_source:
            lo, hi = p.get("x_search_min_seconds", 180), p.get("x_search_max_seconds", 300)
            tasks.append(self.supervise("x-sweep", lambda: self.x_step("sweep"), lambda: random.uniform(lo, hi)))
            tasks.append(self.supervise("x-watch", lambda: self.x_step("watch"),
                                        lambda: jittered(p.get("x_watch_seconds", 240), j)))
        else:
            log.info("X source not configured (add accounts with: python main.py x-login)")
        if self.telegram and self.telegram.configured():
            tasks.append(self.telegram_run())
        else:
            log.info("Telegram source not configured (TELEGRAM_API_ID/HASH + channels.yaml)")
        if self.fomo:
            tasks.append(self.supervise("fomo", self.fomo_step, lambda: jittered(p.get("fomo_hours", 6) * 3600, j)))
        await asyncio.gather(*tasks)
