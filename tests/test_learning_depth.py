"""Deeper learning: candle shapes, rug warning signs, time of day, market activity,
chatter from all of X + token-page comments, and the rug spotter."""

import httpx

from backtest import engine
from charts import candle_shape
from config import Secrets
from db import DB
from dryrun import synthetic_history
from graph import Graph, parse_comments
from pipeline import Pipeline
from safety import FAIL, PASS, WARN, SafetyReport
from scoring import Assessment, Verdict, market_features, should_alert
from sources.dex_source import MarketInfo, parse_pair
from tests.helpers import cfg, http_with, run
from text_analysis import keyword_reading


def test_candle_shape_reads_green_candles_wicks_spikes_and_higher_lows():
    # ts, open, high, low, close, volume - a steady climb with one big volume spike at the end
    c = [(i, 1 + i * 0.1, 1.15 + i * 0.1, 0.95 + i * 0.1, 1.1 + i * 0.1, 100) for i in range(11)]
    c.append((11, 2.1, 2.9, 2.05, 2.8, 1000))
    f = candle_shape(c)
    assert f["green_ratio"] == 1.0
    assert f["higher_lows"] == 6
    assert f["volume_spike"] == 10.0
    assert round(f["biggest_candle_pct"]) == 33 and round(f["last_candle_pct"]) == 33
    assert 0 < f["upper_wick"] < 1 and 0 < f["lower_wick"] < 1
    assert candle_shape(c[:2]) == {}  # too little data: nothing, not garbage


def test_dexscreener_m5_h6_fields_are_parsed():
    m = parse_pair({"chainId": "solana", "baseToken": {"address": "So11111111111111111111111111111111111111112"},
                    "txns": {"m5": {"buys": 30, "sells": 10}}, "priceChange": {"m5": 12.5, "h6": -40}})
    assert (m.buys_m5, m.sells_m5, m.price_change_m5, m.price_change_h6) == (30, 10, 12.5, -40)


def test_features_include_rug_signs_market_activity_and_time_of_day():
    r = SafetyReport("solana", "A")
    r.add("liquidity", PASS, "")
    r.add("lp_locked", WARN, "")
    r.add("contract_controls", FAIL, "")
    m = MarketInfo("solana", "A", liquidity_usd=10000, fdv=200000, volume_h1=50000, buys_m5=30, sells_m5=10)
    a = Assessment("solana", "A", r, market=m)
    a.seen_at = 1_700_000_000  # 2023-11-14 22:13 UTC, a Tuesday
    a.x_mentions, a.comments = 7, 12
    f = a.features()
    assert (f["safety_liquidity"], f["safety_lp_locked"], f["safety_contract_controls"]) == (0, 1, 2)
    assert f["buy_sell_m5"] == 3.0 and f["fdv_to_liquidity"] == 20.0 and f["volume_h1_to_liquidity"] == 5.0
    assert (f["hour_utc"], f["weekday"]) == (22, 1)
    assert (f["x_mentions"], f["comments"]) == (7, 12)
    assert market_features(MarketInfo("solana", "A")) == {}  # missing data stays missing


def test_keyword_reading_is_free_and_catches_scam_talk():
    s, hype, flags = keyword_reading(["this is a rug, dev sold", "rug rug, avoid", "total scam, dev sold everything"])
    assert s < 0 and any("rug talk" in f for f in flags) and any("dev selling" in f for f in flags)
    s, hype, flags = keyword_reading(["LFG 100x gem 🚀🚀", "sending to the moon lfg"])
    assert s > 0 and hype == 1.0 and flags == []
    assert keyword_reading(["gm"]) == (None, None, [])


def test_token_page_comments_are_parsed_defensively():
    assert parse_comments({"replies": [{"text": " based dev "}, {"text": ""}, {"nope": 1}, "junk"]}) == ["based dev"]
    assert parse_comments([{"content": "cto incoming"}]) == ["cto incoming"]
    assert parse_comments(None) == [] and parse_comments({"error": "x"}) == []


def test_pumpfun_comments_are_fetched_once_and_cached():
    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, json={"replies": [{"text": "dev locked lp, solid"}]})

    g = Graph(DB(), http_with(handler), cfg(), discovery=None, x=None)
    assert run(g.pumpfun_comments("MINTpump")) == ["dev locked lp, solid"]
    assert run(g.pumpfun_comments("MINTpump")) == ["dev locked lp, solid"]
    assert len(calls) == 1 and "/replies/MINTpump" in calls[0]


def test_rug_label_counts_pulled_liquidity_and_90pct_crashes():
    def row(**outs):
        return {"outcomes": {h: o for h, o in outs.items()}}
    assert engine.rug_label(row(**{"1h": {"rugged": 1, "max_drawdown": -0.2}})) == 1
    assert engine.rug_label(row(**{"6h": {"rugged": 0, "max_drawdown": -0.95}})) == 1  # dev dump, liquidity kept
    assert engine.rug_label(row(**{"6h": {"rugged": 0, "max_drawdown": -0.3}})) is None  # 24h not in yet
    assert engine.rug_label(row(**{"24h": {"rugged": 0, "max_drawdown": -0.3}})) == 0


def test_backtest_trains_a_rug_spotter_and_reports_hours(tmp_path):
    db = DB()
    synthetic_history(db, n=180)
    model_path = tmp_path / "model.pkl"
    rep = engine.run(db, cfg(), model_path)
    assert rep["rug_ml"].get("chosen") and engine.rug_model_path(model_path).exists()
    assert rep["rug_signals"] and rep["hours"]
    assert {"safety_lp_locked", "hour_utc"} <= set(engine.signal_keys(engine.load_rows(db)))  # new keys learned
    text = engine.format_report(rep)
    assert "Rug warning signs" in text and "Time of day" in text and "rug spotter" in text


def test_high_rug_risk_blocks_an_ai_pick():
    r = SafetyReport("solana", "A")
    r.add("liquidity", PASS, "")
    a = Assessment("solana", "A", r)
    a.verdict, a.backing, a.ml_prob = Verdict("UNCONFIRMED"), 0.2, 0.9
    c = {"alerts": {"min_backing_to_alert": 1.0, "ai_pick_min_prob": 0.65, "ai_pick_max_rug_prob": 0.4}}
    assert should_alert(a, {"dexscreener"}, c)[0] is True
    a.rug_prob = 0.7
    assert should_alert(a, {"dexscreener"}, c)[0] is False


class BusyX:
    budget, used_this_hour = 150, 100

    def __init__(self):
        self.searches = 0

    async def search(self, q, limit):
        self.searches += 1
        return []


def test_x_chatter_on_ordinary_coins_leaves_budget_for_finding_coins():
    x = BusyX()
    pipe = Pipeline(cfg(), DB(), http_with(lambda r: httpx.Response(404)), Secrets(), x_client=x, console_only=True)
    assert run(pipe._x_chatter("A", keep_budget=True)) is None and x.searches == 0
    assert run(pipe._x_chatter("A", keep_budget=False)) == [] and x.searches == 1  # promising coins always
