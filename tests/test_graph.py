import time

from db import DB, Sighting
from discovery import Discovery
from graph import Graph, handle_from_url
from sources.x_source import XTweet, XUser
from tests.helpers import cfg, http_with, run

import httpx

CA = "6ce9TvjRyG4XEwjEcm16AXyf2hxrXtCEsth429Uk7MwU"


class X:
    def __init__(self, users, pinned=None, tweets=None, followers=None):
        self.users, self.pinned, self.tweets, self.fol = users, pinned or {}, tweets or {}, followers or {}

    async def user_by_handle(self, h):
        return next((u for u in self.users if u.handle.lower() == h.lower()), None)

    async def tweet(self, tid):
        return self.pinned.get(tid)

    async def user_tweets(self, uid, limit):
        return self.tweets.get(uid, [])

    async def followers(self, uid, limit):
        return self.fol.get(uid, [])

    async def following(self, uid, limit):
        return []


def u(uid, handle, bio="", pinned=None, age_days=400, statuses=500, followers=5000):
    return XUser(uid, handle, bio=bio, pfp="https://p/x.jpg", followers=followers, statuses=statuses,
                 created_at=time.time() - age_days * 86400, pinned_ids=pinned or [])


def graph(x, signals=None):
    c = cfg()
    c["signals"] = signals or []
    db = DB()
    return db, Graph(db, http_with(lambda r: httpx.Response(404)), c, Discovery(db, c), x)


def test_handle_from_url():
    assert handle_from_url("https://x.com/goodcat") == "goodcat"
    assert handle_from_url("https://twitter.com/elon/status/123") == "elon"
    assert handle_from_url("https://x.com/i/communities/123") is None
    assert handle_from_url("https://t.me/foo") is None


def test_fake_link_is_not_confirmed():
    """A token page linking a famous account that never posted/linked the CA."""
    db, g = graph(X([u("1", "famous")]))
    rep = run(g.analyse(CA, "solana", {"x": ["https://x.com/famous"]}, "raydium", None))
    assert rep.creator is None and rep.fake_links == ["famous"]
    assert "possible fake link: @famous" in rep.warnings


def test_creator_confirmed_by_bio_pinned_or_posting():
    db, g = graph(X([u("1", "bio", bio=f"CA {CA}")]))
    assert run(g.analyse(CA, "solana", {"x": ["https://x.com/bio"]}, "raydium", None)).creator.handle == "bio"
    pin = XTweet("9", u("2", "pin"), f"official CA {CA}", time.time())
    db, g = graph(X([u("2", "pin", pinned=["9"])], pinned={"9": pin}))
    assert run(g.analyse(CA, "solana", {"x": ["https://x.com/pin"]}, "raydium", None)).creator.handle == "pin"
    db, g = graph(X([u("3", "poster")]))
    db.add_sighting(Sighting(CA, "x", "t1", author="poster", author_id="3"))
    rep = run(g.analyse(CA, "solana", {}, "raydium", None))
    assert rep.creator.handle == "poster" and rep.linked[0].how == "posted the CA"


def test_renamed_account_flagged_as_hijack():
    db, g = graph(X([]))
    for days, handle in ((20, "oldname"), (2, "newname")):
        db.x("INSERT INTO account_snapshots (user_id, taken_at, handle, name, statuses) VALUES ('7', ?, ?, 'n', 500)",
             (time.time() - days * 86400, handle))
    flags = run(g.hijack_flags(u("7", "newname")))
    assert any("renamed from @oldname" in f for f in flags)


def test_mass_deleted_and_recent_crypto_flags():
    db, g = graph(X([], tweets={"8": [XTweet(str(i), u("8", "h"), "buy $CAT CA now", time.time() - 86400) for i in range(5)]
                                     + [XTweet("x", u("8", "h"), "my garden photos", time.time() - 400 * 86400)]}))
    for days, st in ((10, 3000), (1, 40)):
        db.x("INSERT INTO account_snapshots (user_id, taken_at, handle, name, statuses) VALUES ('8', ?, 'h', 'n', ?)",
             (time.time() - days * 86400, st))
    flags = run(g.hijack_flags(u("8", "h", age_days=2000)))
    assert any("mass-deleted" in f for f in flags)
    assert any("only started posting crypto" in f for f in flags)


def test_followed_by_and_interactions_from_cache():
    db, g = graph(X([]), signals=[{"handle": "mega", "x_user_id": "1", "tier": 1},
                                  {"handle": "t2", "x_user_id": "2", "tier": 2}])
    db.x("INSERT INTO follow_edges VALUES ('1', '5', 0)")
    db.x("INSERT INTO follow_edges VALUES ('2', '5', 0)")
    db.x("INSERT INTO tweets (tweet_id, user_id, created_at, reply_to_user_id) VALUES ('t', '1', ?, '5')",
         (time.time() - 2 * 86400,))
    names, n2 = g.followed_by("5")
    assert names == ["mega"] and n2 == 1
    inter = g.interactions("5")
    assert inter[0]["handle"] == "mega" and inter[0]["kind"] == "reply" and inter[0]["tier"] == 1


def test_deployer_history_flags():
    db, g = graph(None)
    summary, flags = g.deployer_history("D", "A", [{"complete": False}] * 12)
    assert "12 pump.fun launches, 0 graduated" in summary and flags
