"""Connections analysis.

a) Who is behind the token: X accounts linked from the DexScreener profile,
   pump.fun metadata socials, and the first X account to post the CA. An account
   is only a "confirmed creator/affiliate" if it posted the CA itself or links
   the token in its bio / pinned tweet - otherwise "possible fake link" (anyone
   can put a famous account on their token page) and it gets no score.
   Plus the deployer wallet's past launches and how they ended.
b) Social graph cache: FOLLOWING lists of tier-1 (every 3 days) and top tier-2
   (weekly) accounts, refreshed slowly one account at a time.
c) Bought / hijacked account detection: recent renames, old accounts that only
   recently started posting crypto, mass-deleted tweets. Any flag cancels the
   connection bonus and adds a warning (and is DANGER when hijack_is_danger).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from db import DB
from discovery import Discovery
from net import Http
from sources.x_source import XUnavailable, XUser
from verify import ca_in_text

log = logging.getLogger(__name__)

CRYPTO_RE = re.compile(
    r"(\bca\b|contract|pump\.fun|dexscreener|solana|\bsol\b|\$[a-z]{2,10}\b|memecoin|\btoken\b|airdrop|"
    r"presale|launch|\d{3,4}x\b|\bape\b|degen|raydium|jupiter|\bmint\b)", re.I)


@dataclass
class LinkedAccount:
    handle: str
    via: str                  # dexscreener | pumpfun | first_poster
    user: XUser | None = None
    confirmed: bool = False
    how: str = ""


@dataclass
class ConnectionReport:
    linked: list[LinkedAccount] = field(default_factory=list)
    creator: XUser | None = None
    tier1_followers: list[str] = field(default_factory=list)
    tier2_followers: int = 0
    interactions: list[dict] = field(default_factory=list)   # {handle, tier, kind, days_ago}
    follower_quality: dict | None = None
    hijack_flags: list[str] = field(default_factory=list)
    fake_links: list[str] = field(default_factory=list)
    deployer: str | None = None
    deployer_summary: str | None = None
    deployer_flags: list[str] = field(default_factory=list)
    note: str | None = None

    @property
    def warnings(self) -> list[str]:
        return self.hijack_flags + [f"possible fake link: @{h}" for h in self.fake_links] + self.deployer_flags


def parse_comments(data, limit: int = 50) -> list[str]:
    """Comment texts from a replies response ({"replies": [...]}, a bare list, ...)."""
    items = data.get("replies") or data.get("data") or data.get("comments") if isinstance(data, dict) else data
    out = []
    for it in items if isinstance(items, list) else []:
        text = (it.get("text") or it.get("content") or it.get("body")) if isinstance(it, dict) else None
        if isinstance(text, str) and text.strip():
            out.append(text.strip()[:500])
    return out[:limit]


def handle_from_url(url: str) -> str | None:
    """x.com/<handle> -> handle. Status / community / intent links are not accounts."""
    try:
        p = urlparse(url if "://" in url else "https://" + url)
    except ValueError:
        return None
    if p.netloc.lower().removeprefix("www.") not in ("x.com", "twitter.com", "mobile.twitter.com"):
        return None
    parts = [s for s in p.path.split("/") if s]
    if not parts or parts[0].lower() in ("i", "intent", "search", "home", "hashtag", "share"):
        return None
    if len(parts) > 1 and parts[1] == "status":
        return parts[0]  # a tweet by that account - still points at the account
    h = parts[0].lstrip("@")
    return h if re.fullmatch(r"[A-Za-z0-9_]{1,15}", h) else None


class Graph:
    def __init__(self, db: DB, http: Http, cfg: dict, discovery: Discovery, x=None):
        self.db = db
        self.http = http
        self.cfg = cfg.get("graph") or {}
        self.discovery = discovery
        self.x = x
        self._cache: dict[str, tuple[float, dict]] = {}
        self._reports: dict[str, tuple[float, "ConnectionReport"]] = {}
        self._comments: dict[str, tuple[float, list[str]]] = {}

    # --- a) who is behind the token ------------------------------------------
    async def pumpfun_meta(self, mint: str) -> dict | None:
        base = self.cfg.get("pumpfun_api", "https://frontend-api-v3.pump.fun")
        r = await self.http.get_json(f"{base}/coins/{mint}")
        return r.data if r.ok and isinstance(r.data, dict) else None

    async def pumpfun_comments(self, mint: str) -> list[str] | None:
        """Comments / theses people left on the token's pump.fun page (cached per coin).
        Untrusted text: it only goes to text analysis, which treats it as data."""
        hit = self._comments.get(mint)
        if hit and time.time() - hit[0] < float(self.cfg.get("comments_cache_minutes", 30)) * 60:
            return hit[1]
        base = self.cfg.get("pumpfun_api", "https://frontend-api-v3.pump.fun")
        r = await self.http.get_json(f"{base}/replies/{mint}", params={"limit": 50, "offset": 0}, retries=0)
        if not r.ok:
            return None
        texts = parse_comments(r.data)
        self._comments[mint] = (time.time(), texts)
        return texts

    async def confirm(self, acc: LinkedAccount, address: str) -> None:
        u = acc.user
        if not u:
            return
        if self.db.q1("SELECT 1 FROM sightings WHERE address = ? AND author_id = ?", (address, u.id)):
            acc.confirmed, acc.how = True, "posted the CA"
        elif ca_in_text(address, u.bio + " " + " ".join(u.bio_links)):
            acc.confirmed, acc.how = True, "CA in bio"
        elif u.pinned_ids and self.x:
            try:
                pinned = await self.x.tweet(u.pinned_ids[0])
            except XUnavailable:
                pinned = None
            if pinned and ca_in_text(address, pinned.all_text()):
                acc.confirmed, acc.how = True, "CA in pinned tweet"

    async def linked_accounts(self, address: str, links: dict, pump: dict | None) -> list[LinkedAccount]:
        found: dict[str, LinkedAccount] = {}
        for url in links.get("x") or []:
            h = handle_from_url(url)
            if h:
                found.setdefault(h.lower(), LinkedAccount(h, "dexscreener"))
        if pump and pump.get("twitter"):
            h = handle_from_url(str(pump["twitter"]))
            if h:
                found.setdefault(h.lower(), LinkedAccount(h, "pumpfun"))
        first_x = self.db.q1("""SELECT author, author_id FROM sightings WHERE address = ? AND source = 'x'
                                AND author_id IS NOT NULL ORDER BY seen_at LIMIT 1""", (address,))
        if first_x and first_x["author"]:
            found.setdefault(first_x["author"].lower(), LinkedAccount(first_x["author"], "first_poster"))
        out = list(found.values())[:4]
        if self.x:
            for acc in out:
                try:
                    acc.user = await self.x.user_by_handle(acc.handle)
                except XUnavailable:
                    break
                if acc.user:
                    self.discovery.observe_user(acc.user)
                    await self.confirm(acc, address)
        return out

    def deployer_history(self, deployer: str, address: str, pump_created: list[dict] | None) -> tuple[str, list[str]]:
        rows = self.db.q("SELECT chain, address FROM tokens WHERE deployer = ? AND address != ?", (deployer, address))
        rugged = good = 0
        for r in rows:
            o = self.db.q1("SELECT rugged, final_return FROM outcomes WHERE address = ? AND horizon = '24h'", (r["address"],))
            if o:
                rugged += bool(o["rugged"])
                good += (o["final_return"] or 0) > 0 and not o["rugged"]
        parts = [f"{len(rows)} earlier launch(es) seen by this bot ({rugged} rugged, {good} up at 24h)"]
        flags = []
        if rugged >= int(self.cfg.get("deployer_rug_flag", 2)):
            flags.append(f"deployer rugged {rugged} earlier tokens")
        if pump_created is not None:
            grads = sum(1 for c in pump_created if c.get("complete"))
            parts.append(f"{len(pump_created)} pump.fun launches, {grads} graduated")
            if len(pump_created) >= int(self.cfg.get("serial_launcher_min", 10)) and grads == 0:
                flags.append(f"serial launcher: {len(pump_created)} pump.fun coins, none graduated")
        return "; ".join(parts), flags

    async def pumpfun_created(self, creator: str) -> list[dict] | None:
        base = self.cfg.get("pumpfun_api", "https://frontend-api-v3.pump.fun")
        r = await self.http.get_json(f"{base}/coins/user-created-coins/{creator}",
                                     params={"offset": 0, "limit": 50, "includeNsfw": "true"})
        if not r.ok:
            return None
        data = r.data.get("coins") if isinstance(r.data, dict) else r.data
        return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else None

    # --- b) social graph cache ----------------------------------------------
    def top_tier2(self) -> list[str]:
        ids = self.discovery.ids_with_tier(2)
        return ids[: int(self.cfg.get("top_tier2", 50))]

    def due_refresh(self) -> str | None:
        now = time.time()
        for ids, days in ((self.discovery.ids_with_tier(1), self.cfg.get("tier1_refresh_days", 3)),
                          (self.top_tier2(), self.cfg.get("tier2_refresh_days", 7))):
            for uid in ids:
                row = self.db.q1("SELECT refreshed_at FROM graph_refresh WHERE user_id = ?", (uid,))
                if not row or now - row["refreshed_at"] >= float(days) * 86400:
                    return uid
        return None

    async def refresh_one(self) -> str | None:
        """Refresh ONE stale following list (the monitor calls this slowly)."""
        if not self.x:
            return None
        uid = self.due_refresh()
        if not uid:
            return None
        following = await self.x.following(uid, int(self.cfg.get("max_following", 500)))
        now = time.time()
        with self.db.conn:
            self.db.conn.execute("DELETE FROM follow_edges WHERE follower_id = ?", (uid,))
            self.db.conn.executemany(
                "INSERT OR IGNORE INTO follow_edges (follower_id, followee_id, fetched_at) VALUES (?, ?, ?)",
                [(uid, f.id, now) for f in following])
            self.db.conn.execute("INSERT OR REPLACE INTO graph_refresh (user_id, refreshed_at) VALUES (?, ?)", (uid, now))
        log.info("graph: cached %d follows of %s", len(following), uid)
        return uid

    def followed_by(self, user_id: str) -> tuple[list[str], int]:
        t1 = set(self.discovery.ids_with_tier(1))
        t2 = set(self.discovery.ids_with_tier(2))
        followers = {r["follower_id"] for r in self.db.q("SELECT follower_id FROM follow_edges WHERE followee_id = ?", (user_id,))}
        names = [self.discovery.tier(u).handle or u for u in sorted(followers & t1)]
        return names, len(followers & t2)

    def interactions(self, user_id: str) -> list[dict]:
        since = time.time() - float(self.cfg.get("interaction_days", 90)) * 86400
        rows = self.db.q("""SELECT user_id, created_at, reply_to_user_id, quoted_user_id, retweeted_user_id
                            FROM tweets WHERE created_at >= ? AND user_id != ? AND
                            (reply_to_user_id = ? OR quoted_user_id = ? OR retweeted_user_id = ?)""",
                         (since, user_id, user_id, user_id, user_id))
        out = []
        for r in rows:
            ti = self.discovery.tier(r["user_id"])
            if ti.tier not in (1, 2):
                continue
            kind = "reply" if r["reply_to_user_id"] == user_id else "quote" if r["quoted_user_id"] == user_id else "retweet"
            out.append({"handle": ti.handle or r["user_id"], "tier": ti.tier, "kind": kind,
                        "days_ago": round((time.time() - r["created_at"]) / 86400, 1)})
        return sorted(out, key=lambda i: i["days_ago"])

    async def follower_quality(self, user: XUser) -> dict | None:
        if not self.x:
            return None
        sample = await self.x.followers(user.id, int(self.cfg.get("follower_sample", 100)))
        if not sample:
            return None
        n = len(sample)
        new = sum(1 for f in sample if f.age_days is not None and f.age_days < 30)
        return {"sample": n,
                "no_pfp_pct": round(100 * sum(f.default_pfp for f in sample) / n, 1),
                "zero_tweets_pct": round(100 * sum(f.statuses == 0 for f in sample) / n, 1),
                "new_pct": round(100 * new / n, 1)}

    # --- c) hijacked / bought accounts -----------------------------------------
    async def hijack_flags(self, user: XUser) -> list[str]:
        flags = []
        rename_s = float(self.cfg.get("rename_days", 30)) * 86400
        snaps = self.db.q("SELECT handle, name, statuses, taken_at FROM account_snapshots WHERE user_id = ? ORDER BY taken_at",
                          (user.id,))
        for prev, cur in zip(snaps, snaps[1:]):
            if time.time() - cur["taken_at"] <= rename_s and prev["handle"] and prev["handle"] != cur["handle"]:
                flags.append(f"@{cur['handle']} was renamed from @{prev['handle']} recently")
            if prev["statuses"] and cur["statuses"] is not None and cur["statuses"] < prev["statuses"] * 0.5 \
                    and prev["statuses"] >= 50:
                flags.append(f"@{user.handle} mass-deleted tweets ({prev['statuses']} -> {cur['statuses']})")
        age = user.age_days or 0
        if age > 730 and user.statuses < 20 and user.followers > 1000:
            flags.append(f"@{user.handle}: {age / 365:.0f}y old, {user.followers} followers but only {user.statuses} tweets "
                         "(likely wiped)")
        if age > 365 and self.x:
            try:
                tweets = await self.x.user_tweets(user.id, 40)
            except XUnavailable:
                tweets = []
            crypto = [t.created_at for t in tweets if CRYPTO_RE.search(t.text or "")]
            other = [t.created_at for t in tweets if not CRYPTO_RE.search(t.text or "")]
            recent = time.time() - 30 * 86400
            if crypto and min(crypto) >= recent and other and min(other) < recent:
                flags.append(f"@{user.handle}: {age / 365:.0f}y-old account only started posting crypto in the last 30 days")
        return sorted(set(flags))

    # --- entry point ------------------------------------------------------
    async def analyse(self, address: str, chain: str, links: dict, dex_id: str | None,
                      deployer: str | None) -> ConnectionReport:
        """Cached per token for `report_cache_minutes` so repeat sightings don't burn the X budget."""
        hit = self._reports.get(address)
        if hit and time.time() - hit[0] < float(self.cfg.get("report_cache_minutes", 60)) * 60:
            rep = hit[1]
            if rep.creator:  # cheap, local: refresh from the cache tables
                rep.tier1_followers, rep.tier2_followers = self.followed_by(rep.creator.id)
                rep.interactions = self.interactions(rep.creator.id)
            return rep
        rep = await self._analyse(address, chain, links, dex_id, deployer)
        self._reports[address] = (time.time(), rep)
        return rep

    async def _analyse(self, address: str, chain: str, links: dict, dex_id: str | None,
                       deployer: str | None) -> ConnectionReport:
        rep = ConnectionReport(deployer=deployer)
        pump = await self.pumpfun_meta(address) if chain == "solana" and (dex_id in ("pumpfun", "pumpswap") or address.endswith("pump")) else None
        if pump and pump.get("creator") and not rep.deployer:
            rep.deployer = pump["creator"]
        if rep.deployer:
            created = await self.pumpfun_created(rep.deployer) if chain == "solana" else None
            rep.deployer_summary, rep.deployer_flags = self.deployer_history(rep.deployer, address, created)
        if not self.x:
            rep.note = "X not configured - creator/graph analysis skipped"
            rep.linked = await self.linked_accounts(address, links, pump)
            return rep
        try:
            rep.linked = await self.linked_accounts(address, links, pump)
        except XUnavailable as exc:
            rep.note = f"X unavailable: {exc}"
            return rep
        rep.fake_links = [a.handle for a in rep.linked if a.user and not a.confirmed and a.via != "first_poster"]
        confirmed = [a for a in rep.linked if a.confirmed]
        if not confirmed:
            return rep
        creator = confirmed[0].user
        rep.creator = creator
        cached = self._cache.get(creator.id)
        if cached and time.time() - cached[0] < float(self.cfg.get("cache_hours", 12)) * 3600:
            rep.follower_quality, rep.hijack_flags = cached[1]["fq"], cached[1]["hj"]
        else:
            try:
                rep.hijack_flags = await self.hijack_flags(creator)
                rep.follower_quality = await self.follower_quality(creator)
            except XUnavailable as exc:
                rep.note = f"X unavailable: {exc}"
            self._cache[creator.id] = (time.time(), {"fq": rep.follower_quality, "hj": rep.hijack_flags})
        rep.tier1_followers, rep.tier2_followers = self.followed_by(creator.id)
        rep.interactions = self.interactions(creator.id)
        return rep
