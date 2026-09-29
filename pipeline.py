"""Glue: sighting -> resolve on DexScreener -> safety -> verdict -> alert."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from alerts import AlertContent, Alerter
from config import Secrets
from db import DB, Sighting
from extract import Candidate, extract
from net import Http
from safety import SafetyChecker, SafetyReport
from scoring import DANGER, UNCHECKED, Verdict, verdict
from sources.dex_source import DexScreener

log = logging.getLogger(__name__)


@dataclass
class Result:
    address: str
    status: str  # alerted | not_alerted | no_pair | resolve_failed
    chain: str | None = None
    verdict: Verdict | None = None
    safety: SafetyReport | None = None


class Pipeline:
    def __init__(self, cfg: dict, db: DB, http: Http, secrets: Secrets, console_only: bool = False):
        self.cfg = cfg
        self.db = db
        self.http = http
        self.dex = DexScreener(http, cfg["chains"])
        self.safety = SafetyChecker(http, cfg["safety"])
        self.alerter = Alerter(http, secrets, cfg, db, console_only=console_only)

    # --- entry points -------------------------------------------------------
    async def process_text(self, text: str, source: str, source_ref: str,
                           author: str | None = None, url: str | None = None) -> list[Result]:
        results = []
        for cand in extract(text):
            s = Sighting(cand.address, source, source_ref, cand.chain_hint, cand.kind, author,
                         url or cand.url, text)
            results.append(await self.process(cand, s))
        return results

    async def poll_dex(self) -> list[Result]:
        found = await self.dex.poll(self.db)
        if found is None:
            await self._source_failed("dexscreener", "token-profile feed request failed")
            return []
        await self._source_ok("dexscreener")
        results = []
        for sighting, links in found:
            cand = Candidate(sighting.address, sighting.chain_hint, "token", sighting.url)
            results.append(await self.process(cand, sighting, links))
        return results

    # --- core ---------------------------------------------------------------
    async def process(self, cand: Candidate, sighting: Sighting,
                      links: dict[str, list[str]] | None = None) -> Result:
        self.db.add_sighting(sighting)
        market, api_ok = await self.dex.resolve(cand)
        if not api_ok:
            log.warning("could not resolve %s (DexScreener unavailable)", cand.address)
            return Result(cand.address, "resolve_failed")
        if market is None:
            log.info("%s: no DEX pair on watched chains yet", cand.address)
            return Result(cand.address, "no_pair")

        chain, address = market.chain, market.address
        if address != cand.address:  # a DexScreener pair link resolved to its token
            self.db.add_sighting(Sighting(address, sighting.source, sighting.source_ref, chain, "token",
                                          sighting.author, sighting.url, sighting.text, sighting.seen_at))
        links = _merge_links(links, market)
        self.db.upsert_token(chain, address, name=market.name, symbol=market.symbol,
                             pair_address=market.pair_address, dex_url=market.url, links=links)

        report = await self._safety(chain, address, market)
        v = verdict(report)
        self.db.upsert_token(chain, address, last_verdict=v.label)

        if not self._alertable(address, v):
            log.info("%s:%s %s from %s only - logged, not alerted", chain, address, v.label, sighting.source)
            return Result(address, "not_alerted", chain, v, report)

        first = self.db.first_sighting(address)
        content = AlertContent(
            verdict=v, chain=chain, address=address, name=market.name, symbol=market.symbol,
            safety=report,
            source=first["source"] if first else sighting.source,
            source_url=(first["url"] if first else None) or sighting.url,
            dex_url=market.url, links=links,
        )
        sent = await self.alerter.send_verdict(content)
        return Result(address, "alerted" if sent else "not_alerted", chain, v, report)

    async def _safety(self, chain: str, address: str, market) -> SafetyReport:
        max_age = float(self.cfg["safety"].get("recheck_minutes", 30)) * 60
        cached = self.db.latest_safety(chain, address, max_age)
        if cached:
            report = SafetyReport.from_dict(cached)
            report.market = market.to_dict()  # always show fresh market data
            if report.overall != "unknown":
                return report
        report = await self.safety.check(chain, address, market)
        self.db.save_safety(chain, address, report.overall, report.to_dict())
        return report

    def _alertable(self, address: str, v: Verdict) -> bool:
        if v.label not in (DANGER, UNCHECKED):
            return True
        # Unsafe coins only alert when someone is actually pushing them somewhere we watch;
        # otherwise the DexScreener feed alone would flood the channel with rugs.
        allowed = set(self.cfg["alerts"].get("danger_alert_sources") or [])
        return any(s["source"] in allowed for s in self.db.sightings_for(address))

    # --- source health ------------------------------------------------------
    async def _source_ok(self, source: str) -> None:
        if self.db.record_source_ok(source):
            await self.alerter.send_system(f"source '{source}' has recovered")

    async def _source_failed(self, source: str, error: str) -> None:
        n = self.db.record_source_error(source, error)
        limit = int(self.cfg["alerts"].get("source_down_after_failures", 5))
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
