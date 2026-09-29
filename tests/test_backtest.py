import json
import time

import pytest

from backtest import engine
from backtest.outcomes import compute, snapshot
from db import DB
from dryrun import synthetic_history
from papertrade import PaperTrader, qualify
from safety import PASS, SafetyReport
from scoring import Assessment, Verdict
from sources.dex_source import MarketInfo
from tests.helpers import cfg

COSTS = {"fee_pct": 0, "slippage_pct": 0}


def row(first, path, verdict="UNCONFIRMED", backing=2.0):
    return {"first_seen": first, "entry_price": 1.0, "verdict": verdict, "features": {"backing": backing},
            "outcomes": {}, "path": path}


def test_time_split_trains_on_older_coins_only():
    rows = [row(t, []) for t in (5, 1, 4, 2, 3, 9, 7, 8, 6, 10)]
    train, test = engine.time_split(rows, 0.7)
    assert [r["first_seen"] for r in train] == [1, 2, 3, 4, 5, 6, 7]
    assert min(r["first_seen"] for r in test) > max(r["first_seen"] for r in train)


def test_simulate_take_profit_and_stop():
    up = row(0, [[900, 1.2, 0.95, 1.1], [1800, 1.6, 1.1, 1.5]])
    down = row(0, [[900, 1.05, 0.6, 0.7]])
    rule = {"take_profit_pct": 50, "stop_loss_pct": 30}
    assert engine.simulate(up, rule, COSTS) == pytest.approx(0.5)
    assert engine.simulate(down, rule, COSTS) == pytest.approx(-0.3)


def test_stop_assumed_first_when_candle_hits_both():
    both = row(0, [[900, 2.0, 0.5, 1.0]])
    assert engine.simulate(both, {"take_profit_pct": 50, "stop_loss_pct": 30}, COSTS) == pytest.approx(-0.3)


def test_costs_reduce_returns():
    up = row(0, [[900, 1.6, 1.0, 1.5]])
    net = engine.simulate(up, {"take_profit_pct": 50, "stop_loss_pct": 30}, {"fee_pct": 1, "slippage_pct": 2})
    assert net < 0.5 - 0.04


def test_simulate_without_path_is_pessimistic():
    r = {"first_seen": 0, "entry_price": 1, "path": None,
         "outcomes": {"24h": {"max_gain": 1.0, "max_drawdown": -0.4, "final_return": 0.5, "rugged": 0}}}
    assert engine.simulate(r, {"take_profit_pct": 50, "stop_loss_pct": 30}, COSTS) == pytest.approx(-0.3)


def test_matches_rules():
    assert engine.matches({"backing": 2}, "UNCONFIRMED", {"min_verdict": "UNCONFIRMED", "min_backing": 1.5})
    assert not engine.matches({"backing": 1}, "UNCONFIRMED", {"min_backing": 1.5})
    assert not engine.matches({"backing": 9}, "DANGER", {"min_verdict": "UNCONFIRMED"})
    assert not engine.matches({}, "VERIFIED", {"min_global_fees_sol": 1.5})  # unknown fees can't pass a fee filter


def test_metrics_and_drawdown():
    m = engine.metrics([0.5, -0.3, -0.3, 0.5], stake_pct=10)
    assert m["n"] == 4 and m["win_rate"] == 0.5 and round(m["ev"], 3) == 0.1
    assert m["max_drawdown_pct"] > 5


def test_overfit_warnings():
    w = engine.overfit_warnings({"n": 100, "ev": 0.4, "win_rate": 0.8}, {"n": 10, "ev": 0.9, "win_rate": 0.9}, 8)
    joined = " ".join(w)
    assert "too few" in joined and "suspiciously" in joined and "multiple testing" in joined


def test_full_backtest_on_synthetic_history():
    db = DB()
    synthetic_history(db, n=160)
    rep = engine.run(db, cfg())
    assert rep["split"]["train"] == 112 and rep["split"]["test"] == 48
    assert rep["signals"] and rep["fee_thresholds"]["min_global_fees_sol"]
    text = engine.format_report(rep)
    assert "split by time" in text and "Which signals predicted outcomes" in text


def test_outcome_compute():
    res = compute(1.0, [(100, 1, 1.5, 0.8, 1.2, 0), (200, 1.2, 2.0, 1.1, 1.9, 0)], 100, 1000)
    assert res["max_gain"] == 1.0 and round(res["max_drawdown"], 2) == -0.2 and round(res["final_return"], 2) == 0.9


def assessment(price=1.0, backing=2.0, label="UNCONFIRMED"):
    r = SafetyReport("solana", "A")
    r.add("liquidity", PASS, "")
    a = Assessment("solana", "A", r, market=MarketInfo("solana", "A", price_usd=price))
    a.backing, a.verdict = backing, Verdict(label)
    return a


def test_snapshot_schedules_all_horizons_once():
    db = DB()
    assert snapshot(db, assessment(), time.time())
    assert not snapshot(db, assessment(), time.time())
    assert {json.loads(r["payload"])["horizon"] for r in db.q("SELECT payload FROM pending_checks")} == {"15m", "1h", "6h", "24h"}


def test_paper_trading_open_and_exit_rules():
    db = DB()
    c = cfg()
    pt = PaperTrader(db, c)
    assert pt.on_alert(assessment()) == ["backed"]
    assert pt.on_alert(assessment()) == []  # one position per strategy per coin
    assert pt.on_alert(assessment(label="DANGER")) == []
    t = db.q1("SELECT * FROM paper_trades")
    assert t["entry_price"] == 1.02  # slippage applied
    assert pt.evaluate(t, 1.6) == "take profit"
    assert pt.evaluate(t, 0.7) == "stop loss"
    assert pt.evaluate(t, 1.0, now=t["opened_at"] + 25 * 3600) == "max hold time"
    db.x("INSERT INTO exit_flags (chain, address, flag, detail, at) VALUES ('solana', 'A', 'dev_selling', '', ?)",
         (time.time() + 1,))
    assert pt.evaluate(t, 1.0) == "exit warning: dev_selling"
    pnl = pt.close(t, 1.6, "take profit")
    assert round(pnl, 3) == round(1.6 * 0.98 / 1.02 - 1 - 0.02, 3)


def test_qualify_says_no_edge_found():
    db = DB()
    ok, text = qualify(db, cfg())
    assert not ok and "NO EDGE FOUND" in text and "[FAIL]" in text


def test_retrain_runs_in_a_worker_thread_with_its_own_connection(tmp_path):
    """Regression: the daily retrain used the main SQLite connection from another thread and crashed."""
    import asyncio

    from monitor import Monitor

    db = DB(tmp_path / "live.db")
    synthetic_history(db, n=60)

    class P:
        class paper:
            model_path = tmp_path / "model.pkl"
            _ml_loaded_at = 1

    P.db = db
    m = Monitor.__new__(Monitor)
    m.db, m.cfg, m.pipe = db, cfg(), P
    asyncio.run(asyncio.to_thread(m._retrain))  # raised sqlite3.ProgrammingError before the fix
    assert P.paper._ml_loaded_at == 0
