from charts import ath_distance, exit_flags, parse_gt, pct_change, score_chart, structure, summarise, vol_ratio


def candles(closes, vols=None):
    return [(i * 300, c, c * 1.01, c * 0.99, c, (vols or [100] * len(closes))[i]) for i, c in enumerate(closes)]


def test_basic_features():
    c = candles([1, 1, 1, 2], [100, 100, 100, 400])
    assert pct_change(c, 1) == 100
    assert vol_ratio(c) == 4
    assert round(ath_distance(candles([1, 2, 1])), 1) == -50.5


def test_structure():
    up = candles([1, 2, 1.5, 3, 2.5, 4, 3.5])
    down = candles([4, 5, 3, 4, 2, 3, 1])
    assert structure(up) == "higher_highs"
    assert structure(down) == "lower_highs"


def test_entry_quality_labels():
    assert score_chart({"change_1h": 300, "ath_distance_pct": -2})[1].startswith("overextended")
    assert score_chart({"structure": "higher_highs", "ath_distance_pct": -20})[1] == "pullback in an uptrend"
    assert summarise({"change_1h": 12.0, "structure": "higher_highs"}) == "1h +12% | higher highs"


def snap(price, liq, top10=20, holders=1000, buys=100):
    return {"price_usd": price, "liquidity_usd": liq, "top10_pct": top10, "holder_count": holders, "buys_h1": buys}


def test_exit_flags():
    cfg = {}
    flags = dict(exit_flags([snap(1, 100_000), snap(1.2, 40_000, top10=12, holders=800, buys=30)], cfg,
                            deployer_sells=2, smart_exits=[{"wallet": "abc", "label": "whale"}]))
    assert set(flags) == {"dev_selling", "smart_wallets_exiting", "liquidity_removed", "top10_selling",
                          "holder_count_dropping", "buy_volume_fading"}


def test_no_flags_when_healthy():
    assert exit_flags([snap(1, 100_000), snap(1.1, 110_000, buys=120)], {}) == []


def test_parse_gt_sorts_oldest_first():
    data = {"data": {"attributes": {"ohlcv_list": [[2, 1, 1, 1, 1, 1], [1, 1, 1, 1, 1, 1], ["bad"]]}}}
    assert [c[0] for c in parse_gt(data)] == [1.0, 2.0]
    assert parse_gt({"nope": 1}) is None
