"""Exit warnings only for coins you're in; you say so from the ntfy app (button or `in <CA>`)."""
import json

import httpx

from alerts import ntfy_payload
from config import Secrets
from db import DB
from holdings import Holdings, handle_message, parse_command
from pipeline import Pipeline
from tests.helpers import cfg, http_with, run

GCAT = "6ce9TvjRyG4XEwjEcm16AXyf2hxrXtCEsth429Uk7MwU"


def test_commands_from_buttons_and_typing():
    assert parse_command(f"in solana:{GCAT}") == ("in", GCAT, "solana")       # the "I bought" button
    assert parse_command(f"out solana:{GCAT}") == ("out", GCAT, "solana")     # the "I sold" button
    assert parse_command(f"Bought {GCAT}")[:2] == ("in", GCAT)                # typed in the ntfy app
    assert parse_command(f"sold {GCAT}")[:2] == ("out", GCAT)
    assert parse_command(f"  IN   {GCAT}  ")[:2] == ("in", GCAT)
    assert parse_command(f"interesting coin {GCAT}") is None                  # not a command
    assert parse_command("in nothing here") is None
    assert parse_command("⚠️ EXIT WARNING GoodCat") is None                   # the bot's own messages


def test_holdings_add_remove_and_keep_watching_until_you_sell():
    db = DB()
    h = Holdings(db, cfg())
    assert h.add("solana", GCAT) and not h.add("solana", GCAT)
    assert h.holds("solana", GCAT)
    assert h.remove(GCAT) == 1 and not h.holds("solana", GCAT)
    assert h.add("solana", GCAT)  # bought again later
    db.x("UPDATE holdings SET opened_at = 1")
    assert h.holds("solana", GCAT)  # months later: still watched until you say "out"
    assert not Holdings(db, cfg(alerts={"holding_max_days": 14})).holds("solana", GCAT)  # optional time limit


def test_buy_alert_has_an_i_bought_button_that_posts_quietly_to_the_bot():
    p = ntfy_payload("t", "title", "body", "https://fomo.family/x",
                     [("🛒 Open to buy", "https://fomo.family/x"),
                      ("✅ I bought", "https://ntfy.sh/t-holdings", f"in solana:{GCAT}"),
                      ("📈 Chart", "https://dexscreener.com/x")], "UNCONFIRMED")
    kinds = [(a["action"], a["label"]) for a in p["actions"]]
    assert kinds == [("view", "🛒 Open to buy"), ("http", "✅ I bought"), ("view", "📈 Chart")]
    bought = p["actions"][1]
    assert bought["method"] == "POST" and bought["body"] == f"in solana:{GCAT}" and bought["url"].endswith("-holdings")


def make_pipe(posts):
    def handler(req):
        if req.url.host == "ntfy.sh":
            posts.append(json.loads(req.content))
            return httpx.Response(200, json={})
        return httpx.Response(404)

    return Pipeline(cfg(alerts={"desktop": False}), DB(), http_with(handler), Secrets(ntfy_topic="ccw-test"))


def test_in_and_out_messages_update_holdings_and_confirm_quietly():
    posts = []
    pipe = make_pipe(posts)
    pipe.db.upsert_token("solana", GCAT, symbol="GCAT")
    ev = {"event": "message", "id": "m1", "message": f"in solana:{GCAT}"}
    assert run(handle_message(pipe, pipe.holdings, ev)) == f"in {GCAT}"
    assert run(handle_message(pipe, pipe.holdings, ev)) is None  # same message replayed: ignored
    assert pipe.holdings.holds("solana", GCAT)
    assert "Watching $GCAT" in posts[-1]["title"] and posts[-1]["priority"] == 2
    run(handle_message(pipe, pipe.holdings, {"event": "message", "id": "m2", "message": f"sold {GCAT}"}))
    assert not pipe.holdings.holds("solana", GCAT) and "Stopped watching $GCAT" in posts[-1]["title"]
    assert run(handle_message(pipe, pipe.holdings, {"event": "message", "id": "m3", "message": "hello"})) is None


def test_exit_warnings_only_for_coins_you_are_in():
    posts = []
    pipe = make_pipe(posts)
    db = pipe.db
    db.upsert_token("solana", GCAT, symbol="GCAT", name="GoodCat")
    db.record_alert("solana", GCAT, "verdict", "UNCONFIRMED", ["ntfy"])

    async def fake_resolve(c):
        return None, True  # pool gone -> liquidity_removed flag

    pipe.dex.resolve = fake_resolve
    run(pipe.follow_up("solana", GCAT, 0))
    assert not [p for p in posts if "EXIT" in p["title"]]          # alerted, but you're not in it
    db.x("DELETE FROM exit_flags")
    pipe.holdings.add("solana", GCAT)
    run(pipe.follow_up("solana", GCAT, 0))
    (warn,) = [p for p in posts if "EXIT" in p["title"]]
    assert any(a["label"] == "✋ I sold" and a["body"] == f"out solana:{GCAT}" for a in warn["actions"])
