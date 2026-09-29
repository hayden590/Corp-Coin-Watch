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
