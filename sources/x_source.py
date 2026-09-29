"""X (Twitter) via twscrape using burner accounts - no paid API.

Everything outside this file talks to X through `XClient` and the plain
XUser / XTweet dataclasses, so twscrape API drift stays contained here and
tests / dry-run can use a fake client.

Accounts are always matched by numeric user ID, never display name or handle.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Protocol

log = logging.getLogger(__name__)

DEFAULT_QUERIES = ["pump.fun", "dexscreener.com", "CA:", "contract address"]


@dataclass
class XUser:
    id: str
    handle: str
    name: str = ""
    bio: str = ""
    pfp: str = ""
    verified_type: str | None = None  # "business" | "government" | "blue" | None
    followers: int = 0
    statuses: int = 0
    created_at: float | None = None
    website: str | None = None
    bio_links: list[str] = field(default_factory=list)
    pinned_ids: list[str] = field(default_factory=list)

    @property
    def default_pfp(self) -> bool:
        return not self.pfp or "default_profile" in self.pfp

    @property
    def is_gold(self) -> bool:
        return self.verified_type in ("business", "government")

    @property
    def age_days(self) -> float | None:
        return (time.time() - self.created_at) / 86400 if self.created_at else None


@dataclass
class XTweet:
    id: str
    user: XUser
    text: str
    created_at: float
    kind: str = "post"  # post | reply | quote | retweet
    links: list[str] = field(default_factory=list)
    reply_to_user_id: str | None = None
    quoted: "XTweet | None" = None
    retweeted: "XTweet | None" = None

    @property
    def url(self) -> str:
        return f"https://x.com/{self.user.handle}/status/{self.id}"

    def all_text(self) -> str:
        """Own text + expanded links + quoted/retweeted content (where a CA often lives)."""
        parts = [self.text, *self.links]
        for inner in (self.quoted, self.retweeted):
            if inner:
                parts += [inner.text, *inner.links]
        return "\n".join(p for p in parts if p)


class XClient(Protocol):
    async def search(self, query: str, limit: int) -> list[XTweet]: ...
    async def user_tweets(self, user_id: str, limit: int) -> list[XTweet]: ...
    async def user_by_id(self, user_id: str) -> XUser | None: ...
    async def user_by_handle(self, handle: str) -> XUser | None: ...
    async def tweet(self, tweet_id: str) -> XTweet | None: ...
    async def following(self, user_id: str, limit: int) -> list[XUser]: ...
    async def followers(self, user_id: str, limit: int) -> list[XUser]: ...
    async def status(self) -> dict[str, Any]: ...


class XUnavailable(Exception):
    """All X accounts are locked/down (or the hourly budget is spent - see subclass)."""


class XBudgetExceeded(XUnavailable):
    """Our own hourly request budget is used up - not an outage."""


# --- twscrape conversion -------------------------------------------------------

def _ts(v: Any) -> float | None:
    if isinstance(v, datetime):
        return v.timestamp()
    return None


def user_from_twscrape(u: Any) -> XUser:
    links = [getattr(l, "url", None) for l in getattr(u, "descriptionLinks", None) or []]
    links = [l for l in links if l]
    blue_type = (getattr(u, "blueType", None) or "").lower() or None
    verified = blue_type or ("blue" if getattr(u, "blue", False) else None)
    return XUser(
        id=str(getattr(u, "id_str", None) or getattr(u, "id", "")),
        handle=getattr(u, "username", "") or "",
        name=getattr(u, "displayname", "") or "",
        bio=getattr(u, "rawDescription", "") or "",
        pfp=getattr(u, "profileImageUrl", "") or "",
        verified_type=verified,
        followers=int(getattr(u, "followersCount", 0) or 0),
        statuses=int(getattr(u, "statusesCount", 0) or 0),
        created_at=_ts(getattr(u, "created", None)),
        website=links[0] if links else None,
        bio_links=links,
        pinned_ids=[str(i) for i in getattr(u, "pinnedIds", None) or []],
    )


def tweet_from_twscrape(t: Any, depth: int = 0) -> XTweet:
    rt = getattr(t, "retweetedTweet", None)
    qt = getattr(t, "quotedTweet", None)
    reply_to = getattr(t, "inReplyToUser", None)
    kind = "retweet" if rt else "quote" if qt else "reply" if getattr(t, "inReplyToTweetId", None) else "post"
    return XTweet(
        id=str(getattr(t, "id_str", None) or getattr(t, "id", "")),
        user=user_from_twscrape(t.user),
        text=getattr(t, "rawContent", "") or "",
        created_at=_ts(getattr(t, "date", None)) or time.time(),
        kind=kind,
        links=[l.url for l in getattr(t, "links", None) or [] if getattr(l, "url", None)],
        reply_to_user_id=str(getattr(reply_to, "id", "") or "") or None if reply_to else None,
        quoted=tweet_from_twscrape(qt, depth + 1) if qt and depth < 1 else None,
        retweeted=tweet_from_twscrape(rt, depth + 1) if rt and depth < 1 else None,
    )


class TwscrapeClient:
    """Real client. twscrape rotates accounts and cools down locked ones itself;
    we add an hourly request budget and turn "no account available" into XUnavailable."""

    def __init__(self, db_path: Path, requests_per_hour: int = 150, on_request=None):
        from twscrape import API  # imported lazily: optional dependency

        self.api = API(str(db_path), raise_when_no_account=True, wait_timeout=10)
        self.budget = requests_per_hour
        self._window_start = time.time()
        self._used = 0
        self.on_request = on_request

    def _spend(self, n: int = 1) -> None:
        now = time.time()
        if now - self._window_start >= 3600:
            self._window_start, self._used = now, 0
        if self._used + n > self.budget:
            raise XBudgetExceeded(f"hourly X budget of {self.budget} requests used")
        self._used += n
        if self.on_request:
            self.on_request("x.com")

    @property
    def used_this_hour(self) -> int:
        return self._used

    async def _collect(self, agen: AsyncIterator, limit: int, pages: int) -> list:
        from twscrape import NoAccountError

        self._spend(pages)
        out = []
        try:
            async for item in agen:
                out.append(item)
                if len(out) >= limit:
                    break
        except NoAccountError as exc:
            raise XUnavailable(str(exc)) from exc
        return out

    async def _one(self, coro):
        from twscrape import NoAccountError

        self._spend()
        try:
            return await coro
        except NoAccountError as exc:
            raise XUnavailable(str(exc)) from exc

    async def search(self, query: str, limit: int) -> list[XTweet]:
        items = await self._collect(self.api.search(query, limit=limit, kv={"product": "Latest"}), limit, 1)
        return [tweet_from_twscrape(t) for t in items]

    async def user_tweets(self, user_id: str, limit: int) -> list[XTweet]:
        items = await self._collect(self.api.user_tweets_and_replies(int(user_id), limit=limit), limit, 1)
        return [tweet_from_twscrape(t) for t in items]

    async def user_by_id(self, user_id: str) -> XUser | None:
        u = await self._one(self.api.user_by_id(int(user_id)))
        return user_from_twscrape(u) if u else None

    async def user_by_handle(self, handle: str) -> XUser | None:
        u = await self._one(self.api.user_by_login(handle.lstrip("@")))
        return user_from_twscrape(u) if u else None

    async def tweet(self, tweet_id: str) -> XTweet | None:
        t = await self._one(self.api.tweet_details(int(tweet_id)))
        return tweet_from_twscrape(t) if t else None

    async def following(self, user_id: str, limit: int) -> list[XUser]:
        items = await self._collect(self.api.following(int(user_id), limit=limit), limit, max(1, limit // 50))
        return [user_from_twscrape(u) for u in items]

    async def followers(self, user_id: str, limit: int) -> list[XUser]:
        items = await self._collect(self.api.followers(int(user_id), limit=limit), limit, max(1, limit // 50))
        return [user_from_twscrape(u) for u in items]

    async def status(self) -> dict[str, Any]:
        info = await self.api.pool.accounts_info()
        return {
            "accounts": [{"username": a["username"], "active": a["active"], "logged_in": a["logged_in"],
                          "requests": a["total_req"], "error": a["error_msg"]} for a in info],
            "used_this_hour": self._used,
            "budget": self.budget,
        }


async def add_accounts_from_env(db_path: Path, x_accounts: str, x_cookies: str) -> int:
    """`python main.py x-login`: register burner accounts from .env and log them in."""
    from twscrape import API

    api = API(str(db_path))
    n = 0
    for entry in filter(None, (e.strip() for e in x_accounts.split(";"))):
        parts = entry.split(":")
        if len(parts) < 4:
            log.error("X_ACCOUNTS entry needs username:password:email:email_password")
            continue
        await api.pool.add_account(parts[0], parts[1], parts[2], ":".join(parts[3:]))
        n += 1
    # Several accounts are separated by "|" (a cookie string itself contains ";").
    for entry in filter(None, (e.strip() for e in x_cookies.split("|"))):
        username, _, cookies = entry.partition("=")
        if not cookies:
            log.error("X_COOKIES entry needs username=cookie_string")
            continue
        await api.pool.add_account(username, "-", "-", "-", cookies=cookies)
        n += 1
    await api.pool.login_all()
    return n


# --- polling ---------------------------------------------------------------------

class XSource:
    """Search sweeps + watching accounts by ID. Yields unseen tweets."""

    def __init__(self, client: XClient, db, cfg: dict):
        self.client = client
        self.db = db
        self.xcfg = cfg.get("x") or {}

    def _new(self, tweets: list[XTweet]) -> list[XTweet]:
        return [t for t in tweets if self.db.mark_seen("x", t.id)]

    async def _run(self, calls) -> list[XTweet]:
        """Run fetches in turn. If X gives out part-way, keep what was already fetched
        (those tweets are marked seen, so dropping them would lose them for good)."""
        out: list[XTweet] = []
        for make_call in calls:
            try:
                out += self._new(await make_call())
            except XUnavailable:
                if not out:
                    raise
                log.warning("x: stopped early (X unavailable); processing %d tweets already fetched", len(out))
                break
            await asyncio.sleep(random.uniform(2, 6) * float(self.xcfg.get("pace", 1.0)))
        return out

    async def sweep(self) -> list[XTweet]:
        queries = list(self.xcfg.get("queries") or DEFAULT_QUERIES)
        random.shuffle(queries)
        limit = int(self.xcfg.get("search_limit", 20))
        return await self._run([lambda q=q: self.client.search(q, limit) for q in queries])

    async def watch(self, user_ids: list[str]) -> list[XTweet]:
        limit = int(self.xcfg.get("watch_limit", 20))
        return await self._run([lambda u=u: self.client.user_tweets(u, limit) for u in user_ids])
