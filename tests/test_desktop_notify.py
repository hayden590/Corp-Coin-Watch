from alerts import AlertContent, Alerter
from config import Secrets
from db import DB
from desktop_notify import DesktopNotifier, buy_link, click_link
from safety import FAIL, PASS, SafetyReport
from scoring import Assessment, Verdict
from tests.helpers import cfg, http_with, run

import httpx

CA = "6ce9TvjRyG4XEwjEcm16AXyf2hxrXtCEsth429Uk7MwU"
TPL = "https://fomo.example/token/{chain}/{address}"
DEX = "https://dexscreener.com/solana/pair"


def test_buy_link_template():
    assert buy_link(TPL, "solana", CA) == f"https://fomo.example/token/solana/{CA}"
    assert buy_link("", "solana", CA) is None
    assert buy_link("https://x/{nope}", "solana", CA) is None


def test_unsafe_coins_never_link_to_buy_page():
    assert click_link("UNCONFIRMED", TPL, "solana", CA, DEX).startswith("https://fomo.example")
    assert click_link("VERIFIED", TPL, "solana", CA, DEX).startswith("https://fomo.example")
    assert click_link("EXIT", TPL, "solana", CA, DEX).startswith("https://fomo.example")  # to sell
    assert click_link("DANGER", TPL, "solana", CA, DEX) == DEX
    assert click_link("UNCHECKED", TPL, "solana", CA, DEX) == DEX
    assert click_link("UNCONFIRMED", "", "solana", CA, DEX) == DEX  # no template -> chart


def test_mac_and_linux_commands_are_argument_lists():
    n = DesktopNotifier(True)
    cmd = n.command("terminal-notifier", 'Evil"; rm -rf ~', "body", "https://x")
    assert cmd[-2:] == ["-open", "https://x"] and 'Evil"; rm -rf ~' in cmd  # passed as one arg, no shell
    osa = n.command("osascript", 'a "quoted" \\ title', "b", None)[2]
    assert '\\"quoted\\"' in osa and "\\\\" in osa
    assert n.command("notify-send", "t", "b", "https://x")[-1].endswith("https://x")


class Recorder(DesktopNotifier):
    def __init__(self):
        super().__init__(True)
        self.sent = []

    def _send(self, title, body, url):
        self.sent.append((title, body, url))
        return True


def content(label):
    r = SafetyReport("solana", CA, market={"liquidity_usd": 85000, "age_minutes": 180})
    r.add("liquidity", PASS, "")
    if label == "DANGER":
        r.add("honeypot", FAIL, "cannot sell")
    a = Assessment("solana", CA, r)
    a.verdict, a.backing = Verdict(label, ["x"]), 1.5
    return AlertContent(a, "GoodCat", "GCAT", "x", None, DEX, {})


def alerter():
    c = cfg(alerts={"buy_link": TPL, "desktop": True})
    al = Alerter(http_with(lambda r: httpx.Response(404)), Secrets(), c, DB())
    al.desktop = Recorder()
    return al


def test_safe_alert_pops_up_with_buy_link_and_shows_it_in_chat_text():
    al = alerter()
    c = content("UNCONFIRMED")
    assert run(al.send_verdict(c))
    title, body, url = al.desktop.sent[0]
    assert "GoodCat" in title and "buy page" in body and url == buy_link(TPL, "solana", CA)
    assert c.buy_url and any("Open to buy yourself" in l for l in c.link_lines())


def test_danger_alert_pops_up_without_buy_link():
    al = alerter()
    c = content("DANGER")
    run(al.send_verdict(c))
    title, body, url = al.desktop.sent[0]
    assert body.startswith("DO NOT BUY") and url == DEX and c.buy_url is None


def test_desktop_failure_never_raises():
    class Broken(DesktopNotifier):
        def _send(self, *a):
            raise RuntimeError("no display")

    assert run(Broken(True).send("t", "b", None)) is False


def test_telegram_gets_phone_buttons_but_danger_gets_no_buy_button():
    import json as _json
    sent = []

    def handler(req):
        sent.append(_json.loads(req.content))
        return httpx.Response(200, json={"ok": True})

    c = cfg(alerts={"buy_link": TPL, "desktop": False})
    al = Alerter(http_with(handler), Secrets(telegram_bot_token="1:T", telegram_chat_id="9"), c, DB())
    run(al.send_verdict(content("UNCONFIRMED")))
    kb = sent[0]["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in kb] == ["🛒 Open to buy", "📈 Chart"] and kb[0]["url"] == buy_link(TPL, "solana", CA)
    al2 = Alerter(http_with(handler), Secrets(telegram_bot_token="1:T", telegram_chat_id="9"), c, DB())
    run(al2.send_verdict(content("DANGER")))
    assert [b["text"] for b in sent[1]["reply_markup"]["inline_keyboard"][0]] == ["📈 Chart"]


# --- ntfy: server -> phone + laptop ----------------------------------------------

def test_ntfy_payload_click_and_buttons():
    from alerts import ntfy_payload

    p = ntfy_payload("topic", "🟡 UNCONFIRMED GoodCat", "liq $85K", "https://buy", [("🛒 Open to buy", "https://buy"),
                     ("📈 Chart", DEX), ("bad", "javascript:alert(1)")], "UNCONFIRMED")
    assert p["click"] == "https://buy" and p["priority"] == 4 and p["tags"] == ["yellow_circle"]
    assert [a["label"] for a in p["actions"]] == ["🛒 Open to buy", "📈 Chart"]
    assert "click" not in ntfy_payload("t", "x", "y", "file:///etc", [], "DANGER")


def test_alert_goes_to_ntfy_and_danger_has_no_buy_action():
    import json as _json
    posts = []

    def handler(req):
        posts.append(_json.loads(req.content))
        return httpx.Response(200, json={"id": "1"})

    c = cfg(alerts={"buy_link": TPL, "desktop": False})
    sec = Secrets(ntfy_topic="ccw-test")
    assert run(Alerter(http_with(handler), sec, c, DB()).send_verdict(content("UNCONFIRMED")))
    run(Alerter(http_with(handler), sec, c, DB()).send_verdict(content("DANGER")))
    safe, danger = posts
    assert safe["topic"] == "ccw-test" and safe["click"] == buy_link(TPL, "solana", CA)
    assert danger["click"] == DEX and all("buy" not in a["label"].lower() for a in danger.get("actions", []))


def test_laptop_listener_turns_stream_into_popups():
    from laptop_notifier import listen, to_popup

    assert to_popup({"event": "keepalive"}) is None
    assert to_popup({"event": "message", "title": "T", "message": "M", "click": "https://x"}) == ("T", "M", "https://x")
    assert to_popup({"event": "message", "actions": [{"action": "view", "url": "https://y"}]})[2] == "https://y"
    assert to_popup({"event": "message", "click": "file:///etc/passwd"})[2] is None

    lines = "\n".join([
        '{"id":"a","event":"open"}', '{"id":"b","event":"keepalive"}',
        '{"id":"c","event":"message","title":"🟡 GoodCat","message":"liq","click":"https://buy"}'])
    shown = []

    class Rec(DesktopNotifier):
        async def send(self, *a):
            shown.append(a)
            return True

    import laptop_notifier
    real = httpx.AsyncClient

    class FakeClient(real):
        def __init__(self, *a, **kw):
            super().__init__(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=lines)))

    laptop_notifier.httpx.AsyncClient = FakeClient
    try:
        run(listen("https://ntfy.example", "t", notifier=Rec(True), once=True))
    finally:
        laptop_notifier.httpx.AsyncClient = real
    assert shown == [("🟡 GoodCat", "liq", "https://buy")]
