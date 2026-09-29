"""Glue: sighting -> resolve -> safety -> fees -> legitimacy -> wallets ->
connections -> chart -> text -> score/verdict -> snapshot -> alert -> paper trade.

Expensive steps (connections, chart, text) only run for coins that passed basic
safety AND have some social backing, so free API limits go where they matter.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from alerts import AlertContent, Alerter
from backtest import engine, outcomes
from charts import Charts
from config import Secrets
from db import DB, Sighting
from discovery import Discovery
from extract import Candidate, extract
from fees import GlobalFees
from graph import Graph
from net import Http
from papertrade import PaperTrader
from safety import FAIL, SafetyChecker, SafetyReport
from scoring import DANGER, Assessment, Verdict, score, should_alert
from sources.dex_source import DexScreener, MarketInfo
from sources.telegram_source import TgMessage, channel_map
from sources.x_source import XTweet, XUnavailable
from text_analysis import TextAnalyzer
from verify import Verifier, ca_in_text
from wallets import Wallets

log = logging.getLogger(__name__)
SOCIAL = {"x", "telegram", "fomo", "manual"}


@dataclass
class Result:
    address: str
    status: str  # alerted | not_alerted | no_pair | resolve_failed
    chain: str | None = None
    verdict: Verdict | None = None
    safety: SafetyReport | None = None
    assessment: Assessment | None = None
    reason: str | None = None


class Pipeline:
    def __init__(self, cfg: dict, db: DB, http: Http, secrets: Secrets, console_only: bool = False,
                 x_client=None, model_path: Path | None = None):
        self.cfg = cfg
        self.db = db
        self.http = http
        self.x = x_client
        self.dex = DexScreener(http, cfg["chains"])
        self.safety = SafetyChecker(http, cfg["safety"])
        self.alerter = Alerter(http, secrets, cfg, db, console_only=console_only)
        self.discovery = Discovery(db, cfg)
        self.verifier = Verifier(db, http, cfg, self.discovery)
        self.wallets = Wallets(db, http, cfg, secrets.helius_api_key, getattr(secrets, "etherscan_api_key", ""))
        self.fees = GlobalFees(db, http, cfg, secrets.helius_api_key)
        self.graph = Graph(db, http, cfg, self.discovery, x_client)
        self.charts = Charts(db, http, cfg, getattr(secrets, "birdeye_api_key", ""))
        self.text = TextAnalyzer(db, http, cfg, secrets.anthropic_api_key)
        self.paper = PaperTrader(db, cfg, model_path)
        self.channels = channel_map(cfg.get("channels") or [])
        self._x_cache: dict[str, tuple[float, list[str]]] = {}
        self.wallets.sync(cfg)

    # --- entry points -------------------------------------------------------
    async def process_text(self, text: str, source: str, source_ref: str, author: str | None = None,
                           url: str | None = None, author_id: str | None = None) -> list[Result]:
        results = []
        for cand in extract(text):
            s = Sighting(cand.address, source, source_ref, cand.chain_hint, cand.kind, author,
                         url or cand.url, text, author_id=author_id)
            results.append(await self.process(cand, s))
        return results

    async def handle_telegram(self, m: TgMessage) -> list[Result]:
        if not self.db.mark_seen("telegram", m.source_ref):
            return []
        return await self.process_text(m.text, "telegram", m.source_ref, m.label, m.url)

    async def handle_tweet(self, t: XTweet) -> list[Result]:
        u = t.user
        tier = self.discovery.tier(u.id)
        is_org = self.discovery.is_org(u.id)
        text = t.all_text()
        cands = extract(text)
        if cands or tier.tier or is_org:
            self.discovery.observe_user(u)
        if tier.tier or is_org:
            quoted = t.quoted.user.id if t.quoted else None
            rted = t.retweeted.user.id if t.retweeted else None
            self.db.x("""INSERT OR IGNORE INTO tweets (tweet_id, user_id, text, created_at, kind, reply_to_user_id,
                         quoted_user_id, retweeted_user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                      (t.id, u.id, text[:2000], t.created_at, t.kind, t.reply_to_user_id, quoted, rted))
        results = []
        for cand in cands:
            s = Sighting(cand.address, "x", t.id, cand.chain_hint, cand.kind, u.handle, t.url, t.text,
                         seen_at=t.created_at if t.created_at < time.time() else None, author_id=u.id)
            if tier.is_signal:
                # Direct engagement: the CA is in their own post / reply / quote / retweet.
                self.db.x("""INSERT OR IGNORE INTO endorsements (user_id, handle, tier, weight, address, kind, tweet_id, at)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                          (u.id, u.handle, tier.tier, tier.weight, cand.address, t.kind, t.id, t.created_at))
            if tier.is_signal or is_org:
                self.verifier.schedule_persistence(t.id, cand.address, cand.chain_hint, u.id)
                for wallet, chain in self.wallets.signal_wallets(u.id):
                    window = float((self.cfg.get("wallets") or {}).get("dump_window_minutes", 60)) * 60
                    for frac in (0.25, 0.5, 1.0):
                        self.db.schedule("dump_check", f"{wallet}:{cand.address}", t.created_at + window * frac,
                                         chain, cand.address, {"wallet": wallet})
            results.append(await self.process(cand, s))
        return results

    async def poll_dex(self) -> list[Result]:
        found = await self.dex.poll(self.db)
        if found is None:
            await self.source_failed("dexscreener", "token-profile feed request failed")
            return []
        await self.source_ok("dexscreener")
        results = []
        for sighting, links in found:
            cand = Candidate(sighting.address, sighting.chain_hint, "token", sighting.url)
            results.append(await self.process(cand, sighting, links))
        return results

    async def reassess(self, chain: str, address: str) -> Result | None:
        tok = self.db.get_token(chain, address)
        if not tok:
            return None
        links = json.loads(tok["links_json"]) if tok["links_json"] else None
        return await self.process(Candidate(address, chain, "token"), None, links)

    # --- core ---------------------------------------------------------------
    async def process(self, cand: Candidate, sighting: Sighting | None,
                      links: dict[str, list[str]] | None = None) -> Result:
        if sighting:
            self.db.add_sighting(sighting)
        market, api_ok = await self.dex.resolve(cand)
        if not api_ok:
            log.warning("could not resolve %s (DexScreener unavailable)", cand.address)
            return Result(cand.address, "resolve_failed")
        if market is None:
            log.info("%s: no DEX pair on watched chains yet", cand.address)
            return Result(cand.address, "no_pair")

        chain, address = market.chain, market.address
        if sighting and address != cand.address:  # a DexScreener pair link resolved to its token
            self.db.add_sighting(Sighting(address, sighting.source, sighting.source_ref, chain, "token",
                                          sighting.author, sighting.url, sighting.text, sighting.seen_at,
                                          sighting.author_id))
        tok = self.db.get_token(chain, address)
        if links is None and tok and tok["links_json"]:
            links = json.loads(tok["links_json"])
        links = _merge_links(links, market)
        self.db.upsert_token(chain, address, name=market.name, symbol=market.symbol,
                             pair_address=market.pair_address, dex_url=market.url, links=links)

        report = await self._safety(chain, address, market)
        if report.deployer:
            self.db.upsert_token(chain, address, deployer=report.deployer)
        self.charts.snapshot_market(market, report.holder_count, report.top10_pct)
        a = Assessment(chain, address, report, market)
        basic_ok = not any(c.status == FAIL for c in report.checks)
        a.legit = await self.verifier.assess(address, market.name, market.symbol, links.get("website") or [])
        a.smart_buys = self.wallets.smart_buys(address)
        a.dump_flags = self.wallets.dump_flags(address)
        first = self.db.first_sighting(address)
        a.seen_at = first["seen_at"] if first else time.time()
        inputs = self._backing_inputs(address)
        score(a, self.cfg, inputs)  # preliminary: decides whether deep analysis is worth the API calls
        self._ai_score(a)

        sources = {s["source"] for s in self.db.sightings_for(address)}
        promising = (sources & SOCIAL or a.backing > 0 or
                     (a.ml_prob is not None and a.ml_prob >= float(self.cfg["alerts"].get("ai_pick_min_prob", 0.65)) - 0.1))
        if basic_ok and not a.verdict.is_danger:
            # What people are saying - all of X plus the comments on the token's own page - for every coin
            # that passes the basic checks, so the AI learns how chatter relates to pumps and rugs.
            x_posts = await self._x_chatter(address, keep_budget=not promising)
            comments = await self._comments(chain, address, market)
            a.x_mentions = len(x_posts) if x_posts is not None else None
            a.comments = len(comments) if comments is not None else None
            label = f"{market.name} ({market.symbol})"
            if promising:
                # Paid / rate-limited lookups only for coins worth it (Helius free tier is ~300 calls/day).
                a.fees = await self.fees.check(market)
                a.connections = await self.graph.analyse(address, chain, links, market.dex_id, report.deployer)
                a.chart = await self.charts.analyse(market)
            a.text = await self.text.analyse(chain, address, label, (x_posts or []) + (comments or []),
                                             use_model=bool(promising))
            score(a, self.cfg, inputs)
            self._ai_score(a)
        a.exit_flags = [(r["flag"], r["detail"]) for r in
                        self.db.q("SELECT flag, detail FROM exit_flags WHERE chain = ? AND address = ?", (chain, address))]
        a.similar = engine.similar_stats(self.db, a.verdict.label, a.backing, chain, self.cfg)

        outcomes.snapshot(self.db, a, a.seen_at)
        self.db.upsert_token(chain, address, last_verdict=a.verdict.label)

        ok, why = should_alert(a, sources, self.cfg)
        if not ok:
            log.info("%s:%s %s - not alerted (%s)", chain, address, a.verdict.label, why)
            return Result(address, "not_alerted", chain, a.verdict, report, a, why)

        content = AlertContent(
            a, market.name, market.symbol,
            source=first["source"] if first else (sighting.source if sighting else "?"),
            source_url=(first["url"] if first else None) or (sighting.url if sighting else None),
            dex_url=market.url, links=links,
        )
        sent = await self.alerter.send_verdict(content)
        if sent:
            if not (tok and tok["alerted_at"]):
                self.db.x("UPDATE tokens SET alerted_at = ? WHERE chain = ? AND address = ?", (time.time(), chain, address))
            self.paper.on_alert(a)
        return Result(address, "alerted" if sent else "not_alerted", chain, a.verdict, report, a,
                      None if sent else "verdict unchanged")

    def _ai_score(self, a: Assessment) -> None:
        """The AIs' chances this coin hits the target / rugs - only from models that proved themselves
        on coins they never trained on."""
        bt = self.cfg.get("backtest") or {}
        gate = (float(bt.get("ml_min_test_auc", 0.6)), int(bt.get("ml_min_train", 150)))
        for attr, model in (("ml_prob", self.paper.ml()), ("rug_prob", self.paper.rug_ml())):
            if model and model.trustworthy(*gate):
                try:
                    setattr(a, attr, round(model.predict(a.features()), 3))
                except Exception as exc:  # a stale model must never break the pipeline
                    log.warning("AI score (%s) failed: %s", attr, exc)

    async def _comments(self, chain: str, address: str, market: MarketInfo) -> list[str] | None:
        """Comments / theses on the token's own page (pump.fun coins)."""
        if chain != "solana" or not (market.dex_id in ("pumpfun", "pumpswap") or address.endswith("pump")):
            return None
        return await self.graph.pumpfun_comments(address)

    async def _x_chatter(self, address: str, keep_budget: bool = False) -> list[str] | None:
        """Recent X posts mentioning this CA from anyone on X (cached 30 min per coin).
        keep_budget: for ordinary coins, only search while most of the hourly X budget is
        unused, so the searches that FIND new coins never starve."""
        if not self.x:
            return None
        hit = self._x_cache.get(address)
        if hit and time.time() - hit[0] < 1800:
            return hit[1]
        budget, used = getattr(self.x, "budget", None), getattr(self.x, "used_this_hour", None)
        share = float((self.cfg.get("x") or {}).get("chatter_budget_share", 0.5))
        if keep_budget and isinstance(budget, (int, float)) and isinstance(used, (int, float)) and used >= budget * share:
            return None
        try:
            tweets = await self.x.search(address, int((self.cfg.get("x") or {}).get("chatter_limit", 25)))
        except XUnavailable as exc:
            log.info("X chatter skipped: %s", exc)
            return None
        tweets = [t for t in tweets if ca_in_text(address, t.all_text())]  # X search is fuzzy: exact CA only
        texts = [t.text for t in tweets if t.text]
        for t in tweets:  # every poster becomes a sighting, so discovery learns who calls coins early
            if self.db.add_sighting(Sighting(address, "x", t.id, None, "token", t.user.handle, t.url, t.text,
                                             author_id=t.user.id)):
                self.discovery.observe_user(t.user)
        self._x_cache[address] = (time.time(), texts)
        return texts

    def _backing_inputs(self, address: str) -> dict:
        endorsements = []
        for e in self.db.q("SELECT * FROM endorsements WHERE address = ? ORDER BY at", (address,)):
            ti = self.discovery.tier(e["user_id"])
            if ti.status == "blacklisted":
                continue
            endorsements.append({"user_id": e["user_id"], "handle": e["handle"], "tier": e["tier"],
                                 "weight": e["weight"], "kind": e["kind"], "promoted": ti.status == "promoted"})
        endorsed = {e["user_id"] for e in endorsements}
        channels, seen = [], set()
        x_posters, blacklisted = set(), set()
        for s in self.db.sightings_for(address):
            if s["source"] == "telegram":
                key = (s["source_ref"] or "").split("/")[0].lower()
                if key in seen:
                    continue
                seen.add(key)
                meta = self.channels.get(key, {"label": s["author"] or key, "weight": 1.0})
                channels.append({"label": meta["label"], "weight": meta["weight"]})
            elif s["source"] == "x" and s["author_id"] and s["author_id"] not in endorsed:
                if self.discovery.tier(s["author_id"]).status == "blacklisted":
                    blacklisted.add(s["author"] or s["author_id"])
                else:
                    x_posters.add(s["author_id"])
        theses = self.db.q1("SELECT COUNT(*) AS n FROM fomo_theses WHERE address = ?", (address,))["n"]
        return {"endorsements": endorsements, "channels": channels, "x_posters": len(x_posters),
                "blacklisted": sorted(blacklisted), "fomo_theses": theses}

    async def _safety(self, chain: str, address: str, market: MarketInfo, force: bool = False) -> SafetyReport:
        max_age = float(self.cfg["safety"].get("recheck_minutes", 30)) * 60
        cached = None if force else self.db.latest_safety(chain, address, max_age)
        if cached:
            report = SafetyReport.from_dict(cached)
            report.market = market.to_dict()  # always show fresh market data
            if report.overall != "unknown":
                return report
        report = await self.safety.check(chain, address, market)
        self.db.save_safety(chain, address, report.overall, report.to_dict())
        return report

    # --- follow-up monitoring (called by monitor.py) ---------------------------
    async def follow_up(self, chain: str, address: str, alerted_at: float) -> list[tuple[str, str]]:
        """Refresh an alerted coin and send EXIT WARNING alerts for new exit flags."""
        from charts import exit_flags

        tok = self.db.get_token(chain, address)
        market, ok = await self.dex.resolve(Candidate(address, chain, "token"))
        if not ok:
            return []
        if market is not None:
            report = await self._safety(chain, address, market)
            self.charts.snapshot_market(market, report.holder_count, report.top10_pct)
        deployer_sells = 0
        if tok and tok["deployer"]:
            act = await self.wallets.fetch_activity(tok["deployer"], chain)
            deployer_sells = sum(1 for x in act or [] if x["side"] == "sell" and x["token"] == address and x["at"] >= alerted_at)
        smart = self.wallets.exits_since(address, alerted_at)
        snaps = self.charts.snapshots(chain, address, alerted_at - 600)
        flags = exit_flags(snaps, self.cfg.get("charts") or {}, deployer_sells, smart)
        if market is None and tok:
            flags.append(("liquidity_removed", "pool no longer listed on DexScreener"))
        new = []
        for flag, detail in flags:
            if self.db.x("INSERT OR IGNORE INTO exit_flags (chain, address, flag, detail, at) VALUES (?, ?, ?, ?, ?)",
                         (chain, address, flag, detail, time.time())):
                new.append((flag, detail))
        if new and (self.cfg.get("alerts") or {}).get("exit_warnings", True) \
                and self.db.last_alert_verdict(chain, address) not in (None, DANGER):
            title = f"{tok['name'] or '?'} (${tok['symbol']})" if tok else address
            if await self.alerter.send_exit_warning(chain, address, title, new, tok["dex_url"] if tok else None):
                self.db.x("UPDATE exit_flags SET alerted = 1 WHERE chain = ? AND address = ?", (chain, address))
        return new

    # --- source health ------------------------------------------------------
    async def source_ok(self, source: str) -> None:
        if self.db.record_source_ok(source):
            await self.alerter.send_system(f"source '{source}' has recovered")

    async def source_failed(self, source: str, error: str, limit: int | None = None) -> None:
        n = self.db.record_source_error(source, error)
        limit = limit or int(self.cfg["alerts"].get("source_down_after_failures", 5))
        if n >= limit and self.db.mark_source_down_alerted(source):
            await self.alerter.send_system(
                f"source '{source}' is DOWN ({n} consecutive failures: {error}). Other sources keep running.")


def _merge_links(links: dict[str, list[str]] | None, market) -> dict[str, list[str]]:
    out = {k: list(v) for k, v in (links or {}).items()}
    for w in market.websites:
        out.setdefault("website", [])
        if w not in out["website"]:
            out["website"].append(w)
    for s in market.socials:
        key = {"twitter": "x", "x": "x", "telegram": "telegram"}.get(s["type"], "other")
        out.setdefault(key, [])
        if s["url"] not in out[key]:
            out[key].append(s["url"])
    return out
