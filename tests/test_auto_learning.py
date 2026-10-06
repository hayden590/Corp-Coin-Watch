"""Auto-found wallets (any trading app - it reads the chain), Helius budget, AI picks."""
import time

import httpx

from db import DB
from safety import PASS, SafetyReport
from scoring import Assessment, Verdict, should_alert
from tests.helpers import cfg, http_with, run
from wallets import HeliusBudget, WalletRecord, Wallets

TOKEN = "6ce9TvjRyG4XEwjEcm16AXyf2hxrXtCEsth429Uk7MwU"
POOL = "9TUYNnUzwyQnGfMyMAAarvK5qrqrYevPphXjcBEvsfqv"


def fake_swap_tx(buyer, ts, sig):
    return {"signature": sig, "timestamp": ts, "feePayer": buyer,
            "tokenTransfers": [{"mint": TOKEN, "toUserAccount": buyer, "fromUserAccount": "pool"}]}


def test_helius_budget_caps_daily_calls():
    HeliusBudget.limit, HeliusBudget._day, HeliusBudget.used = 2, "", 0
    assert HeliusBudget.take() and HeliusBudget.take() and not HeliusBudget.take()
    HeliusBudget.limit = 300


def test_discovers_early_buyers_of_a_winner_but_skips_snipers():
    first = time.time() - 7200
    txs = [fake_swap_tx("sniper", first + 5, "s1"), fake_swap_tx("early1", first + 120, "s2"),
           fake_swap_tx("early2", first + 600, "s3"), fake_swap_tx("late", first + 5000, "s4")]
    HeliusBudget._day, HeliusBudget.used = "", 0
    db = DB()
    w = Wallets(db, http_with(lambda r: httpx.Response(200, json=list(reversed(txs)))), cfg(), "key")
    db.x("INSERT INTO feature_snapshots VALUES ('solana', ?, 0, ?, 1.0, 'UNCONFIRMED', '{}')", (TOKEN, first))
    db.upsert_token("solana", TOKEN, pair_address=POOL, symbol="GCAT")
    db.x("INSERT INTO outcomes (chain, address, horizon, recorded_at, max_gain, max_drawdown, final_return, rugged) "
         "VALUES ('solana', ?, '1h', ?, 2.5, -0.1, 1.8, 0)", (TOKEN, time.time()))
    added = run(w.discover())
    assert set(added) == {"early1", "early2"}  # sniper (first 60s) and late buyer excluded
    rows = db.q("SELECT wallet, label, source FROM wallets")
    assert all(r["source"] == "auto" and "GCAT" in r["label"] for r in rows)
    assert db.q1("SELECT COUNT(*) AS n FROM wallet_activity WHERE token = ?", (TOKEN,))["n"] == 2
    assert run(w.discover()) == []  # each winner is mined once


def test_prune_keeps_the_best_auto_wallets():
    db = DB()
    w = Wallets(db, http_with(lambda r: httpx.Response(404)), cfg(wallets={"max_auto_wallets": 1}), "key")
    for name in ("a", "b"):
        db.x("INSERT INTO wallets (wallet, chain, source) VALUES (?, 'solana', 'auto')", (name,))
    db.x("INSERT INTO wallet_activity VALUES ('a', 'solana', 'T', 'buy', 1, 1, 'x')")
    db.x("INSERT INTO outcomes (chain, address, horizon, recorded_at, max_gain, max_drawdown, final_return, rugged) "
         "VALUES ('solana', 'T', '24h', 0, 1.0, 0, 0.5, 0)")
    assert w.prune_auto() == 1
    assert [r["wallet"] for r in db.q("SELECT wallet FROM wallets")] == ["a"]


def assessment(prob, backing=0.0, trusted=False):
    r = SafetyReport("solana", "A")
    r.add("liquidity", PASS, "")
    a = Assessment("solana", "A", r)
    a.verdict, a.backing, a.ml_prob = Verdict("UNCONFIRMED"), backing, prob
    if trusted:
        a.smart_buys = [{"record": WalletRecord("w", "solana", "x", "auto", 20, 20, 0.5, 0, 1, 1.2), "sold": False,
                         "wallet": "w", "label": "x"}]
    return a


def test_ai_pick_needs_a_confident_model_and_a_hard_signal():
    c = {"alerts": {"min_backing_to_alert": 1.0, "ai_pick_min_prob": 0.65}}
    assert should_alert(assessment(0.8, trusted=True), {"dexscreener"}, c) == (True, "AI pick (80%)")
    assert should_alert(assessment(0.8, backing=0.2), {"dexscreener"}, c)[0] is True
    assert should_alert(assessment(0.8), {"dexscreener"}, c)[0] is False     # model alone never alerts
    assert should_alert(assessment(0.5, trusted=True), {"dexscreener"}, c)[0] is False
    assert should_alert(assessment(None, trusted=True), {"dexscreener"}, c)[0] is False  # no proven model


def test_untrusted_model_is_ignored():
    from backtest.engine import MLModel

    m = MLModel(None, [], {}, "gb")
    assert not m.trustworthy()
    m.test_auc, m.n_train = 0.7, 400
    assert m.trustworthy()


def test_routine_checks_leave_the_reserve_for_discovery():
    HeliusBudget.limit, HeliusBudget.reserve, HeliusBudget._day, HeliusBudget.used = 5, 3, "", 0
    assert HeliusBudget.take(HeliusBudget.reserve) and HeliusBudget.take(HeliusBudget.reserve)
    assert not HeliusBudget.take(HeliusBudget.reserve)       # fees / polling stop here
    assert HeliusBudget.take(0) and HeliusBudget.take(0) and HeliusBudget.take(0)  # discovery can still go
    assert not HeliusBudget.take(0)
    HeliusBudget.limit, HeliusBudget.reserve, HeliusBudget.used = 300, 60, 0


def test_rising_coin_gets_its_early_buyers_noted_and_discovery_uses_them_without_helius():
    first = time.time() - 900
    txs = [fake_swap_tx("sniper", first + 5, "s1"), fake_swap_tx("early1", first + 120, "s2")]
    calls = []

    def handler(req):
        calls.append(req.url)
        return httpx.Response(200, json=list(reversed(txs)))

    HeliusBudget._day, HeliusBudget.used = "", 0
    db = DB()
    w = Wallets(db, http_with(handler), cfg(), "key")
    db.x("INSERT INTO feature_snapshots VALUES ('solana', ?, 0, ?, 1.0, 'UNCONFIRMED', '{}')", (TOKEN, first))
    db.upsert_token("solana", TOKEN, pair_address=POOL, symbol="GCAT")
    db.x("INSERT INTO outcomes (chain, address, horizon, recorded_at, max_gain, max_drawdown, final_return, rugged) "
         "VALUES ('solana', ?, '15m', ?, 0.1, -0.1, 0.05, 0)", (TOKEN, time.time()))
    assert run(w.capture_early("solana", TOKEN)) == 0 and not calls  # only +10%: not worth a Helius call
    db.x("UPDATE outcomes SET max_gain = 0.6 WHERE address = ?", (TOKEN,))
    assert run(w.capture_early("solana", TOKEN)) == 1                 # early1 noted, sniper skipped
    assert run(w.capture_early("solana", TOKEN)) == 0                 # once per coin
    n_calls = len(calls)
    db.x("INSERT INTO outcomes (chain, address, horizon, recorded_at, max_gain, max_drawdown, final_return, rugged) "
         "VALUES ('solana', ?, '6h', ?, 3.0, -0.1, 2.0, 0)", (TOKEN, time.time()))
    assert run(w.discover()) == ["early1"] and len(calls) == n_calls  # it ran: wallet added, no new Helius call


def test_discovery_retries_a_winner_when_helius_was_unavailable():
    first = time.time() - 7200
    HeliusBudget._day, HeliusBudget.used = "", 0
    db = DB()
    w = Wallets(db, http_with(lambda r: httpx.Response(503)), cfg(), "key")  # Helius down
    db.x("INSERT INTO feature_snapshots VALUES ('solana', ?, 0, ?, 1.0, 'UNCONFIRMED', '{}')", (TOKEN, first))
    db.upsert_token("solana", TOKEN, pair_address=POOL, symbol="GCAT")
    db.x("INSERT INTO outcomes (chain, address, horizon, recorded_at, max_gain, max_drawdown, final_return, rugged) "
         "VALUES ('solana', ?, '1h', ?, 2.5, -0.1, 1.8, 0)", (TOKEN, time.time()))
    assert run(w.discover()) == []
    assert not db.q1("SELECT 1 FROM seen_items WHERE source = 'wallet_mine'")  # not marked: retried next run


def test_random_x_posters_alone_never_trigger_an_alert():
    r = SafetyReport("solana", "A")
    r.add("liquidity", PASS, "")
    a = Assessment("solana", "A", r)
    a.verdict = Verdict("UNCONFIRMED")
    a.backing, a.backing_breakdown = 1.0, [("5 other X poster(s)", 1.0)]
    c = {"alerts": {"min_backing_to_alert": 1.0}}
    ok, why = should_alert(a, {"x"}, c)
    assert not ok and "random X posters" in why
    a.backing, a.backing_breakdown = 2.0, [("5 other X poster(s)", 1.0), ("telegram Alpha Calls", 1.0)]
    assert should_alert(a, {"x", "telegram"}, c) == (True, "backing 1.0")


def test_daily_learning_report_reads_like_a_progress_check(tmp_path):
    from backtest import engine
    from dryrun import synthetic_history
    from papertrade import learning_summary

    db = DB()
    synthetic_history(db, n=180)
    engine.run(db, cfg(), tmp_path / "model.pkl")
    title, body = learning_summary(db, tmp_path / "model.pkl", cfg())
    assert "learning report" in title
    assert "Studied 180 coins" in body and "Winner-finder AI: test score" in body and "Rug-spotter AI" in body
    assert "Paper trades" in body and "no real money" in body


def test_daily_report_goes_out_once_a_day_as_a_quiet_ntfy_message():
    from config import Secrets
    from monitor import Monitor
    from pipeline import Pipeline

    posts = []

    def handler(req):
        import json as _json
        posts.append(_json.loads(req.content))
        return httpx.Response(200, json={})

    c = cfg(alerts={"daily_report_hour": 0, "desktop": False})
    pipe = Pipeline(c, DB(), http_with(handler), Secrets(ntfy_topic="ccw-test"))
    mon = Monitor(pipe, c)
    run(mon._daily_report())
    run(mon._daily_report())
    assert len(posts) == 1 and posts[0]["priority"] == 2 and "learning report" in posts[0]["title"]
    assert "click" not in posts[0]  # a report never carries a buy link
