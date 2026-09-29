import json
import logging

import httpx

from alerts import AlertContent, Alerter, format_discord, format_telegram, format_text
from config import SecretRedactor, Secrets
from db import DB
from safety import FAIL, PASS, SafetyReport
from scoring import DANGER, UNCONFIRMED, Assessment, Verdict
from tests.helpers import cfg, http_with, run


def content(label=UNCONFIRMED, name="Cat", reasons=None):
    r = SafetyReport("solana", "A", market={"liquidity_usd": 50000, "age_minutes": 30, "fdv": 1e6})
    r.add("liquidity", PASS, "$50.0K")
    if label == DANGER:
        r.add("honeypot", FAIL, "cannot sell")
    a = Assessment("solana", "A", r)
    a.verdict = Verdict(label, reasons or ["x"])
    return AlertContent(a, name, "CAT", "x", "https://x.com/a/status/1", "https://dexscreener.com/solana/p",
                        {"x": ["https://x.com/cat"]})


def test_danger_says_do_not_buy_everywhere():
    c = content(DANGER, reasons=["honeypot: cannot sell"])
    assert "DO NOT BUY" in format_text(c)
    assert "DO NOT BUY" in format_telegram(c)
    assert "DO NOT BUY" in format_discord(c)["embeds"][0]["title"]


def test_checks_have_emoji():
    t = format_text(content(DANGER))
    assert "✅ Liquidity" in t and "❌ Honeypot" in t


def test_telegram_escapes_token_names():
    t = format_telegram(content(name="<b>evil</b><a href='x'>"))
    assert "<b>evil" not in t and "&lt;b&gt;evil" in t


def test_discord_disables_mentions():
    d = format_discord(content(name="@everyone"))
    assert d["allowed_mentions"] == {"parse": []}


def _alerter(handler, db, **secret_kw):
    secrets = Secrets(**secret_kw)
    return Alerter(http_with(handler, None), secrets, cfg(), db)


def test_realert_only_on_verdict_change():
    posts = []
    db = DB()
    a = _alerter(lambda req: posts.append(req) or httpx.Response(204), db,
                 discord_webhook_url="https://discord.com/api/webhooks/1/abc")
    assert run(a.send_verdict(content()))
    assert not run(a.send_verdict(content()))
    assert run(a.send_verdict(content(DANGER)))
    assert len(posts) == 2


def test_failed_delivery_not_recorded():
    db = DB()
    a = _alerter(lambda req: httpx.Response(500), db, discord_webhook_url="https://discord.com/api/webhooks/1/abc")
    assert not run(a.send_verdict(content()))
    assert db.last_alert_verdict("solana", "A") is None


def test_telegram_payload():
    sent = []
    db = DB()
    a = _alerter(lambda req: sent.append(json.loads(req.content)) or httpx.Response(200, json={"ok": True}), db,
                 telegram_bot_token="123:TOKEN", telegram_chat_id="42")
    run(a.send_verdict(content()))
    assert sent[0]["chat_id"] == "42" and sent[0]["parse_mode"] == "HTML"


def test_secrets_never_in_repr():
    s = Secrets(discord_webhook_url="https://discord.com/api/webhooks/1/SUPERSECRET", telegram_bot_token="999:BOTTOKEN")
    assert "SUPERSECRET" not in repr(s) and "BOTTOKEN" not in str(s)


def test_redactor_scrubs_logs(caplog):
    s = Secrets(telegram_bot_token="999:BOTTOKENVALUE")
    logger = logging.getLogger("redact-test")
    handler = caplog.handler
    handler.addFilter(SecretRedactor(s))
    logger.error("calling https://api.telegram.org/bot%s/sendMessage", s.telegram_bot_token)
    assert "BOTTOKENVALUE" not in caplog.text and "***" in caplog.text
