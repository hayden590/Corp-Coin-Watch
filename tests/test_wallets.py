import time

from db import DB, Sighting
from tests.helpers import cfg, http_with, run
from wallets import Wallets, parse_evm_tokentx, parse_helius_swaps

import httpx

W = "7nvw5VToQS4K4ApicM3rDoL36xzo1dgcR1wfaeGmY6Uz"
T = "6ce9TvjRyG4XEwjEcm16AXyf2hxrXtCEsth429Uk7MwU"


def test_parse_helius_swaps_skips_quote_tokens():
    txs = [{"signature": "s1", "timestamp": 100, "tokenTransfers": [
        {"mint": T, "toUserAccount": W, "fromUserAccount": "pool", "tokenAmount": 10},
        {"mint": "So11111111111111111111111111111111111111112", "fromUserAccount": W, "toUserAccount": "pool"}]}]
    assert parse_helius_swaps(W, txs) == [{"token": T, "side": "buy", "amount": 10.0, "at": 100.0, "tx": "s1"}]


def test_parse_evm_tokentx():
    rows = [{"hash": "h", "timeStamp": "5", "from": "0xpool", "to": "0xW", "contractAddress": "0xT", "value": "2000",
             "tokenDecimal": "3"}]
    assert parse_evm_tokentx("0xw", rows)[0] == {"token": "0xt", "side": "buy", "amount": 2.0, "at": 5.0, "tx": "h"}


def make(c=None):
    c = c or cfg()
    db = DB()
    return db, Wallets(db, http_with(lambda r: httpx.Response(404)), c, "key")


def test_wallet_needs_history_before_it_counts():
    db, w = make()
    db.x("INSERT INTO wallets (wallet, chain, label, source) VALUES (?, 'solana', 'whale', 'fomo')", (W,))
    for i in range(12):
        tok = f"T{i}"
        w.store(W, "solana", [{"token": tok, "side": "buy", "amount": 1, "at": 100 + i, "tx": f"t{i}"}])
        if i < 9:  # only 9 resolved -> not enough (min_history 10)
            db.x("INSERT INTO outcomes (chain, address, horizon, recorded_at, max_gain, max_drawdown, final_return, rugged) "
                 "VALUES ('solana', ?, '24h', 0, 0.9, -0.1, 0.5, 0)", (tok,))
    rec = w.record(W, "solana")
    assert rec.resolved == 9 and not rec.trusted  # leaderboard rank alone is never trusted
    db.x("INSERT INTO outcomes (chain, address, horizon, recorded_at, max_gain, max_drawdown, final_return, rugged) "
         "VALUES ('solana', 'T9', '24h', 0, 0.0, -0.5, -0.5, 1)")
    rec = w.record(W, "solana")
    assert rec.trusted and rec.win_rate == 0.9 and rec.rug_rate == 0.1


def test_dump_on_followers_flag():
    c = cfg()
    c["signals"] = [{"handle": "caller", "x_user_id": "42", "tier": 2, "wallets": [{"address": W, "chain": "solana"}]}]
    db, w = make(c)
    w.sync(c)
    posted = time.time() - 3600
    db.add_sighting(Sighting(T, "x", "tw1", author="caller", author_id="42", seen_at=posted))
    w.store(W, "solana", [{"token": T, "side": "buy", "amount": 1, "at": posted - 600, "tx": "b"},
                          {"token": T, "side": "sell", "amount": 1, "at": posted + 20 * 60, "tx": "s"}])
    flags = w.dump_flags(T)
    assert flags and "SOLD 20 min after posting" in flags[0]


def test_sell_outside_window_not_flagged():
    c = cfg()
    c["signals"] = [{"handle": "caller", "x_user_id": "42", "tier": 2, "wallets": [{"address": W, "chain": "solana"}]}]
    db, w = make(c)
    w.sync(c)
    posted = time.time() - 10 * 3600
    db.add_sighting(Sighting(T, "x", "tw1", author_id="42", seen_at=posted))
    w.store(W, "solana", [{"token": T, "side": "sell", "amount": 1, "at": posted + 5 * 3600, "tx": "s"}])
    assert w.dump_flags(T) == []


def test_no_helius_key_is_skipped_not_crashed():
    db = DB()
    w = Wallets(db, http_with(lambda r: httpx.Response(500)), cfg(), "")
    assert run(w.fetch_activity(W, "solana")) is None
