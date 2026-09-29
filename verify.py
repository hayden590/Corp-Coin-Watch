"""Legitimacy checks (pass / warn / fail / unknown):

a) tweet_persists     source tweet from an org/signal account re-fetched after a delay;
                      deleted => FAIL ("likely hacked account") => DANGER
b) account_integrity  any change to handle/name/bio/pfp/verified type in the last 72h => FAIL
c) official_website   exact CA found on the org's official domain => strong pass (counts
                      toward VERIFIED); found only on the project's own DexScreener site =>
                      pass but NOT official; not found => warn
d) first_time_poster  the X account never posted a CA before => warn
e) narrative          token name matches a recent tier-1 tweet but no tier-1 account
                      engaged with the CA itself => warn "HIGH RISK"
"""
from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from db import DB
from discovery import Discovery, domain_of
from net import Http
from safety import FAIL, PASS, UNKNOWN, WARN, Check

log = logging.getLogger(__name__)

NARRATIVE_STOPWORDS = {
    "the", "coin", "token", "inu", "sol", "eth", "base", "moon", "pump", "meme", "cat", "dog", "ai",
    "official", "new", "one", "and", "for", "with", "this", "that", "just", "now", "today",
}


@dataclass
class LegitReport:
    checks: list[Check] = field(default_factory=list)
    official_confirmed: bool = False
    persisted: bool | None = None  # None = no relevant tweet / not checked yet

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(name, status, detail))

    def get(self, name: str) -> Check | None:
        return next((c for c in self.checks if c.name == name), None)

    def failed(self, name: str) -> bool:
        c = self.get(name)
        return bool(c and c.status == FAIL)


def is_public_http_url(url: str) -> bool:
    """Only fetch public http(s) sites - never localhost / LAN (URLs come from token metadata)."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    host = p.hostname.lower()
    if host in ("localhost",) or host.endswith((".local", ".internal", ".lan")):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_global
    except ValueError:
        return "." in host


def ca_in_text(address: str, text: str) -> bool:
    if address.startswith("0x"):
        return address.lower() in text.lower()
    return re.search(rf"(?<![1-9A-HJ-NP-Za-km-z]){re.escape(address)}(?![1-9A-HJ-NP-Za-km-z])", text) is not None


class Verifier:
    def __init__(self, db: DB, http: Http, cfg: dict, discovery: Discovery):
        self.db = db
        self.http = http
        self.cfg = cfg.get("verify") or {}
        self.discovery = discovery

    # --- scheduling ---------------------------------------------------------
    def schedule_persistence(self, tweet_id: str, address: str, chain: str | None, author_id: str) -> None:
        delay = float(self.cfg.get("persistence_delay_minutes", 15)) * 60
        self.db.schedule("tweet_persistence", tweet_id, time.time() + delay, chain, address,
                         {"author_id": author_id})

    async def run_persistence_check(self, row, x_client) -> str:
        """Called by the monitor when a check is due. Returns exists | deleted | error."""
        try:
            t = await x_client.tweet(row["ref"])
        except Exception as exc:  # X down / budget spent: retry later
            log.warning("persistence check %s failed: %s", row["ref"], exc)
            return "error"
        return "exists" if t else "deleted"

    # --- the checks ---------------------------------------------------------
    def relevant_x_sightings(self, address: str) -> list:
        """X sightings by org / signal / promoted accounts."""
        out = []
        for s in self.db.sightings_for(address):
            if s["source"] != "x" or not s["author_id"]:
                continue
            if self.discovery.is_org(s["author_id"]) or self.discovery.tier(s["author_id"]).is_signal:
                out.append(s)
        return out

    def persistence(self, address: str, report: LegitReport) -> None:
        rows = self.db.checks_for("tweet_persistence", address)
        if not rows:
            return
        deleted = [r for r in rows if r["result"] == "deleted"]
        pending = [r for r in rows if r["done_at"] is None or r["result"] == "error"]
        if deleted:
            report.persisted = False
            report.add("tweet_persists", FAIL, f"source tweet {deleted[0]['ref']} was DELETED - likely hacked account")
        elif pending:
            mins = max(0, (min(r["due_at"] for r in pending) - time.time()) / 60)
            report.add("tweet_persists", UNKNOWN, f"re-check in {mins:.0f} min")
        else:
            report.persisted = True
            report.add("tweet_persists", PASS, f"{len(rows)} source tweet(s) still up")

    def integrity(self, address: str, report: LegitReport) -> None:
        authors = {s["author_id"] for s in self.relevant_x_sightings(address)}
        if not authors:
            return
        window = float(self.cfg.get("integrity_window_hours", 72)) * 3600
        fields_ = self.cfg.get("integrity_fields") or ["handle", "name", "bio", "pfp", "verified_type"]
        problems, unknown = [], []
        for uid in authors:
            snaps = self.db.q("SELECT * FROM account_snapshots WHERE user_id = ? ORDER BY taken_at", (uid,))
            if len(snaps) < 1:
                unknown.append(uid)
                continue
            cutoff = time.time() - window
            for prev, cur in zip(snaps, snaps[1:]):
                if cur["taken_at"] < cutoff:
                    continue
                changed = [f for f in fields_ if (prev[f] or "") != (cur[f] or "")]
                if changed:
                    handle = cur["handle"] or uid
                    problems.append(f"@{handle} changed {', '.join(changed)} {((time.time() - cur['taken_at']) / 3600):.0f}h ago")
            if len(snaps) == 1 and snaps[0]["taken_at"] > cutoff:
                unknown.append(uid)
        if problems:
            report.add("account_integrity", FAIL, "; ".join(problems[:3]))
        elif unknown and len(unknown) == len(authors):
            report.add("account_integrity", UNKNOWN, "no snapshot history yet")
        else:
            report.add("account_integrity", PASS, "no profile changes in the last "
                       f"{self.cfg.get('integrity_window_hours', 72)}h")

    async def _page_has_ca(self, url: str, address: str) -> bool | None | str:
        """True/False = page checked; None = page missing; "down" = site unreachable."""
        if not is_public_http_url(url):
            return "down"
        cache_s = float(self.cfg.get("website_cache_hours", 6)) * 3600
        row = self.db.q1("SELECT found, checked_at FROM website_checks WHERE address = ? AND url = ?", (address, url))
        if row and time.time() - row["checked_at"] < cache_s:
            return None if row["found"] is None else bool(row["found"])
        r = await self.http.request("GET", url, expect_json=False, headers={"Accept": "text/html,*/*"}, retries=0)
        if r.status is None:
            return "down"
        found = ca_in_text(address, r.data[:2_000_000]) if r.ok and isinstance(r.data, str) else None
        if r.ok or r.status == 404:
            self.db.x("INSERT OR REPLACE INTO website_checks (address, url, found, checked_at) VALUES (?, ?, ?, ?)",
                      (address, url, None if found is None else int(found), time.time()))
        return found

    async def _site_has_ca(self, base: str, address: str) -> tuple[bool, bool]:
        """(found, any_page_reachable)"""
        base = base if "://" in base else "https://" + base
        reachable = False
        for path in self.cfg.get("website_paths") or ["", "/press", "/news", "/token", "/crypto"]:
            found = await self._page_has_ca(urljoin(base.rstrip("/") + "/", path.lstrip("/")), address)
            if found == "down":
                break
            if found is None:
                continue
            reachable = True
            if found:
                return True, True
        return False, reachable

    async def website(self, address: str, dex_websites: list[str], report: LegitReport) -> None:
        org_domains = sorted({d for s in self.relevant_x_sightings(address)
                              for d in self.discovery.org_domains(s["author_id"])})
        for d in org_domains:
            found, _ = await self._site_has_ca(d, address)
            if found:
                report.official_confirmed = True
                report.add("official_website", PASS, f"official site {d} lists this exact CA")
                return
        for w in dex_websites[:2]:
            found, reachable = await self._site_has_ca(w, address)
            if found:
                note = "project's own site lists CA (not an official org confirmation)"
                report.add("official_website", PASS, f"{domain_of(w)}: {note}")
                return
        if org_domains:
            report.add("official_website", WARN, f"CA NOT found on official site {', '.join(org_domains)}")
        elif dex_websites:
            report.add("official_website", WARN, "CA not found on project website")
        else:
            report.add("official_website", WARN, "no website to confirm against")

    def first_time_poster(self, address: str, report: LegitReport) -> None:
        firsts = []
        for s in self.db.sightings_for(address):
            if s["source"] == "x" and s["author_id"]:
                if self.discovery.prior_calls(s["author_id"], s["seen_at"], address) == 0:
                    firsts.append(f"@{s['author'] or s['author_id']}")
        x_posters = {s["author_id"] for s in self.db.sightings_for(address) if s["source"] == "x" and s["author_id"]}
        if not x_posters:
            return
        if firsts and len(set(firsts)) == len(x_posters):
            report.add("first_time_poster", WARN, f"first CA ever from {', '.join(sorted(set(firsts))[:3])}")
        else:
            report.add("first_time_poster", PASS, "poster(s) have prior CA history")

    def narrative(self, address: str, name: str | None, symbol: str | None, report: LegitReport) -> None:
        tier1 = self.discovery.ids_with_tier(1)
        if not tier1:
            return
        since = time.time() - float(self.cfg.get("narrative_window_hours", 48)) * 3600
        marks = ",".join("?" * len(tier1))
        tweets = self.db.q(f"SELECT user_id, text FROM tweets WHERE user_id IN ({marks}) AND created_at >= ?",
                           (*tier1, since))
        terms = narrative_terms(name, symbol)
        hit = next((t for t in tweets if terms and matches_narrative(t["text"] or "", terms)), None)
        if not hit:
            report.add("narrative", PASS, "not riding a tier-1 tweet")
            return
        engaged = self.db.q1("SELECT 1 FROM endorsements WHERE address = ? AND tier = 1", (address,))
        who = self.discovery.tier(hit["user_id"]).handle or hit["user_id"]
        if engaged:
            report.add("narrative", PASS, f"matches @{who}'s tweet AND a tier-1 account engaged with the CA")
        else:
            report.add("narrative", WARN, f"HIGH RISK narrative coin: name matches @{who}'s recent tweet, "
                                          "but no tier-1 account engaged with this CA")

    async def assess(self, address: str, name: str | None, symbol: str | None,
                     dex_websites: list[str]) -> LegitReport:
        report = LegitReport()
        self.persistence(address, report)
        self.integrity(address, report)
        await self.website(address, dex_websites, report)
        self.first_time_poster(address, report)
        self.narrative(address, name, symbol, report)
        return report


def narrative_terms(name: str | None, symbol: str | None) -> list[str]:
    terms = []
    for raw in (symbol, name):
        words = re.sub(r"[^a-z0-9 ]", " ", (raw or "").lower().lstrip("$")).split()
        if all(w in NARRATIVE_STOPWORDS for w in words):
            continue  # e.g. "The Coin" would match everything
        t = " ".join(words)
        if len(t) >= 3:
            terms.append(t)
    return list(dict.fromkeys(terms))


def matches_narrative(text: str, terms: list[str]) -> bool:
    low = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    return any(re.search(rf"\b{re.escape(t)}\b", low) for t in terms)
