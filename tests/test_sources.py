import time
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import httpx

from db import DB
from sources.fomo_source import FomoSource, find_theses, find_wallets
from sources.telegram_source import channel_map, message_text, to_message
from sources.x_source import XSource, XTweet, XUser, tweet_from_twscrape, user_from_twscrape
from tests.helpers import cfg, http_with, run

SOL = "6ce9TvjRyG4XEwjEcm16AXyf2hxrXtCEsth429Uk7MwU"


def ts_user(**kw):
    base = dict(id=42, id_str="42", username="caller", displayname="Caller", rawDescription="bio",
                followersCount=10, statusesCount=5, profileImageUrl="https://p/x.jpg", blueType="Business",
                created=datetime(2020, 1, 1, tzinfo=timezone.utc), descriptionLinks=[NS(url="https://corp.example")],
                pinnedIds=[7])
    base.update(kw)
    return NS(**base)


def test_twscrape_user_conversion():
    u = user_from_twscrape(ts_user())
    assert (u.id, u.handle, u.verified_type, u.website, u.pinned_ids) == ("42", "caller", "business", "https://corp.example", ["7"])
    assert u.is_gold


def test_twscrape_tweet_kinds_and_quoted_ca():
    inner = NS(id_str="1", user=ts_user(id_str="9", username="orig"), rawContent=f"CA {SOL}", date=datetime.now(timezone.utc),
               links=[], retweetedTweet=None, quotedTweet=None, inReplyToTweetId=None, inReplyToUser=None)
    qt = NS(id_str="2", user=ts_user(), rawContent="this 👇", date=datetime.now(timezone.utc), links=[],
            retweetedTweet=None, quotedTweet=inner, inReplyToTweetId=None, inReplyToUser=None)
    t = tweet_from_twscrape(qt)
    assert t.kind == "quote" and SOL in t.all_text() and t.quoted.user.id == "9"


class FakeClient:
    async def search(self, q, limit):
        return [XTweet("1", XUser("5", "a"), "x", time.time())]

    async def user_tweets(self, uid, limit):
        return [XTweet("1", XUser("5", "a"), "x", time.time()), XTweet("2", XUser(uid, "b"), "y", time.time())]


def test_x_source_dedupes_seen_tweets():
    db = DB()
    c = cfg()
    c["x"]["pace"] = 0
    src = XSource(FakeClient(), db, c)
    c["x"]["queries"] = ["q"]
    assert len(run(src.sweep())) == 1
    assert [t.id for t in run(src.watch(["7"]))] == ["2"]


def test_telegram_message_with_hidden_links_and_buttons():
    chans = channel_map([{"channel": "@AlphaCalls", "label": "Alpha", "weight": 2}])
    msg = NS(id=5, raw_text="new call", date=datetime.now(timezone.utc),
             entities=[NS(url=f"https://dexscreener.com/solana/{SOL}")],
             reply_markup=NS(rows=[NS(buttons=[NS(url="https://pump.fun/x")])]))
    m = to_message(msg, NS(username="alphacalls", id=123), chans)
    assert m.label == "Alpha" and m.weight == 2 and m.source_ref == "alphacalls/5"
    assert SOL in m.text and m.url == "https://t.me/alphacalls/5"
    assert to_message(msg, NS(username="other", id=9), chans) is None


def test_fomo_generic_parsing():
    data = {"leaderboard": [{"handle": "degen1", "wallets": {"solanaAddress": SOL}},
                            {"username": "x", "evmWallet": "0x" + "ab" * 20}, {"handle": "nope", "wallet": "garbage"}]}
    ws = find_wallets(data)
    assert {w["wallet"] for w in ws} == {SOL, "0x" + "ab" * 20}
    assert find_theses([{"handle": "a", "thesis": "strong team, real product"}, {"thesis": "short"}]) == \
        [{"author": "a", "text": "strong team, real product"}]


def test_fomo_fallback_modes():
    src = FomoSource(http_with(lambda r: httpx.Response(403)), DB(), cfg())
    assert run(src.refresh_wallets()) == ([], None) and src.mode == "fallback"
    c = cfg(fomo={"leaderboard_url": "https://fomo.example/lb"})
    src = FomoSource(http_with(lambda r: httpx.Response(403)), DB(), c)
    wallets, err = run(src.refresh_wallets())
    assert wallets == [] and "unavailable" in err


def test_x_source_keeps_tweets_fetched_before_outage():
    from sources.x_source import XUnavailable

    class Flaky:
        n = 0

        async def user_tweets(self, uid, limit):
            self.n += 1
            if self.n > 1:
                raise XUnavailable("all accounts locked")
            return [XTweet("1", XUser(uid, "a"), "x", time.time())]

    c = cfg()
    c["x"]["pace"] = 0
    src = XSource(Flaky(), DB(), c)
    assert [t.id for t in run(src.watch(["1", "2", "3"]))] == ["1"]
