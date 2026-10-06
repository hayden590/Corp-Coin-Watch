"""Paper trading with FAKE money, and the `qualify` report card.

HARD RULE: there is no real trading anywhere in this program. A paper trade is
a row in SQLite: simulated buy at the alert-time price plus slippage, exit on
the strategy's take-profit / stop-loss / max-hold, on an exit-warning flag, or
if the coin turns DANGER. Fees and slippage are charged on both sides.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from backtest.engine import MLModel, matches, metrics, rug_model_path
from db import DB
from sources.dex_source import best_pair

log = logging.getLogger(__name__)


class PaperTrader:
    def __init__(self, db: DB, cfg: dict, model_path: Path | None = None):
        self.db = db
        self.strategies = cfg.get("strategies") or []
        bt = cfg.get("backtest") or {}
        self.slip = bt.get("slippage_pct", 2.0) / 100
        self.fee = bt.get("fee_pct", 1.0) / 100
        self.stake = float((cfg.get("paper") or {}).get("stake", 50))
        self.model_path = model_path
        self._ml = None
        self._rug = None
        self._ml_loaded_at = 0.0

    def ml(self) -> MLModel | None:
        if self.model_path and time.time() - self._ml_loaded_at > 3600:
            self._ml, self._ml_loaded_at = MLModel.load(self.model_path), time.time()
            self._rug = MLModel.load(rug_model_path(self.model_path))
        return self._ml

    def rug_ml(self) -> MLModel | None:
        """The rug spotter, trained alongside the main model (same reload schedule)."""
        self.ml()
        return self._rug

    def on_alert(self, a) -> list[str]:
        """Open paper positions for every strategy whose entry rule matches."""
        price = getattr(a.market, "price_usd", None)
        if not price or a.verdict is None or a.verdict.is_danger:
            return []
        feats = a.features()
        model = self.ml()
        prob = model.predict(feats) if model else None
        opened = []
        for s in self.strategies:
            if not matches(feats, a.verdict.label, s, prob):
                continue
            entry = price * (1 + self.slip)
            n = self.db.x("""INSERT OR IGNORE INTO paper_trades (strategy, chain, address, opened_at, entry_price, stake,
                             peak_price) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                          (s["name"], a.chain, a.address, time.time(), entry, self.stake, entry))
            if n:
                opened.append(s["name"])
                log.info("PAPER buy [%s] %s:%s at %.8g (fake money)", s["name"], a.chain, a.address, entry)
        return opened

    def _rule(self, name: str) -> dict:
        return next((s for s in self.strategies if s.get("name") == name), {})

    def close(self, trade, price: float, reason: str) -> float:
        exit_price = price * (1 - self.slip)
        pnl = exit_price / trade["entry_price"] - 1 - 2 * self.fee
        pnl = max(-1.0, pnl)
        self.db.x("""UPDATE paper_trades SET status = 'closed', closed_at = ?, exit_price = ?, exit_reason = ?,
                     pnl_pct = ? WHERE id = ?""", (time.time(), exit_price, reason, pnl * 100, trade["id"]))
        log.info("PAPER sell [%s] %s: %s, %+.1f%% (fake money)", trade["strategy"], trade["address"], reason, pnl * 100)
        return pnl

    def evaluate(self, trade, price: float | None, now: float | None = None) -> str | None:
        """Return an exit reason if the trade should close at `price`."""
        now = now or time.time()
        rule = self._rule(trade["strategy"])
        tok = self.db.q1("SELECT last_verdict FROM tokens WHERE chain = ? AND address = ?", (trade["chain"], trade["address"]))
        if tok and tok["last_verdict"] == "DANGER":
            return "turned DANGER"
        if rule.get("exit_on_flags", True):
            flag = self.db.q1("SELECT flag FROM exit_flags WHERE chain = ? AND address = ? AND at >= ?",
                              (trade["chain"], trade["address"], trade["opened_at"]))
            if flag:
                return f"exit warning: {flag['flag']}"
        if price is None:
            return None
        chg = price / trade["entry_price"] - 1
        if chg >= rule.get("take_profit_pct", 50) / 100:
            return "take profit"
        if chg <= -rule.get("stop_loss_pct", 30) / 100:
            return "stop loss"
        if now - trade["opened_at"] >= rule.get("max_hold_hours", 24) * 3600:
            return "max hold time"
        return None

    async def update(self, dex) -> int:
        closed = 0
        for t in self.db.q("SELECT * FROM paper_trades WHERE status = 'open'"):
            pairs = await dex.token_pairs(t["address"])
            m = best_pair(pairs or [], t["address"], [t["chain"]]) if pairs is not None else None
            price = m.price_usd if m else None
            if pairs is not None and m is None:
                price = 0.0  # pool gone: treat as a total loss
            if price is None:
                continue  # price API down: decide next cycle rather than at a made-up price
            reason = self.evaluate(t, price)
            if reason:
                self.close(t, price, reason)
                closed += 1
            elif price:
                self.db.x("UPDATE paper_trades SET peak_price = MAX(COALESCE(peak_price, 0), ?) WHERE id = ?",
                          (price, t["id"]))
        return closed


def qualify(db: DB, cfg: dict, backtest_report: dict | None = None) -> tuple[bool, str]:
    q = cfg.get("qualify") or {}
    stake_pct = (cfg.get("backtest") or {}).get("stake_pct", 5)
    L = ["QUALIFICATION REPORT CARD (paper trading - fake money only)", ""]
    any_pass = False
    strategies = [s["name"] for s in cfg.get("strategies") or []] or ["-"]
    for name in strategies:
        rows = db.q("SELECT * FROM paper_trades WHERE strategy = ? AND status = 'closed' ORDER BY closed_at", (name,))
        first = db.q1("SELECT MIN(opened_at) AS t FROM paper_trades WHERE strategy = ?", (name,))["t"]
        pnls = [r["pnl_pct"] / 100 for r in rows]
        m = metrics(pnls, stake_pct)
        days = (time.time() - first) / 86400 if first else 0
        crit = [
            (f"at least {q.get('min_trades', 100)} closed paper trades", m["n"] >= q.get("min_trades", 100), f"{m['n']}"),
            (f"over at least {q.get('min_days', 21)} days", days >= q.get("min_days", 21), f"{days:.1f} days"),
            (f"expected value after fees > {q.get('min_ev_pct', 0)}%/trade",
             m["n"] > 0 and m["ev"] * 100 > q.get("min_ev_pct", 0), f"{m['ev'] * 100:+.2f}%" if m["n"] else "n/a"),
            (f"max drawdown < {q.get('max_drawdown_pct', 30)}%",
             m["n"] > 0 and m["max_drawdown_pct"] < q.get("max_drawdown_pct", 30),
             f"{m['max_drawdown_pct']:.1f}%" if m["n"] else "n/a"),
        ]
        ok = all(c[1] for c in crit)
        any_pass |= ok
        L.append(f"Strategy '{name}':")
        for text, passed, val in crit:
            L.append(f"  [{'PASS' if passed else 'FAIL'}] {text:<45} ({val})")
        if m["n"]:
            L.append(f"  win rate {m['win_rate']:.0%}, avg win {m['avg_win']:+.1%}, avg loss {m['avg_loss']:+.1%}")
        open_n = db.q1("SELECT COUNT(*) AS n FROM paper_trades WHERE strategy = ? AND status = 'open'", (name,))["n"]
        L.append(f"  open paper positions: {open_n}")
        L.append("")
    if backtest_report and backtest_report.get("fee_thresholds"):
        L.append("Best global-fees thresholds found by the backtester:")
        for k, v in backtest_report["fee_thresholds"].items():
            if v:
                L.append(f"  {k} >= {v['threshold']:.4g} (test EV {v['test'].get('ev', 0):+.2%}, n={v['test'].get('n', 0)})")
            else:
                L.append(f"  {k}: not enough data yet")
        L.append("")
    if any_pass:
        L.append("Result: at least one strategy meets every criterion on PAPER. That is evidence, not proof.")
        L.append("This program still never trades real money. Keep watching it.")
    else:
        L.append("Result: NO EDGE FOUND.")
        ml = (backtest_report or {}).get("ml") or {}
        if ml.get("chosen"):
            L.append(f"The ML model was retrained on the newest data ({ml['chosen']}); paper trading continues.")
        else:
            reason = ml.get("skipped") or (backtest_report or {}).get("error") or "no backtest data"
            L.append(f"Retraining skipped ({reason}); paper trading continues and it will retry next time.")
        L.append("Do not trust these calls with real money.")
    return any_pass, "\n".join(L)


def learning_summary(db: DB, model_path: Path | None, cfg: dict | None = None) -> tuple[str, str]:
    """Once-a-day "is it actually learning?" report: what it studied, how the AIs test, how the
    fake-money trades went. Returns (title, body)."""
    day = time.time() - 86400
    seen = db.q1("SELECT COUNT(*) AS n FROM feature_snapshots")["n"]
    seen_day = db.q1("SELECT COUNT(*) AS n FROM feature_snapshots WHERE first_seen_at >= ?", (day,))["n"]
    done = db.q1("SELECT COUNT(DISTINCT address) AS n FROM outcomes WHERE horizon = '24h'")["n"]
    lines = [f"Studied {seen} coins (+{seen_day} today), {done} with a full 24h result."]
    if model_path:
        from backtest.engine import MLModel, rug_model_path

        bt = (cfg or {}).get("backtest") or {}
        gate = (float(bt.get("ml_min_test_auc", 0.6)), int(bt.get("ml_min_train", 150)))
        for name, path in (("Winner-finder AI", Path(model_path)), ("Rug-spotter AI", rug_model_path(model_path))):
            m = MLModel.load(path)
            if not m:
                lines.append(f"{name}: not trained yet.")
                continue
            auc = getattr(m, "test_auc", None)
            score = f"test score {auc:.2f}" if auc is not None else "no test score"
            lines.append(f"{name}: {score} on unseen coins (0.5 = guessing) - "
                         + ("in use." if m.trustworthy(*gate) else "not good enough yet, off."))
    closed = db.q("SELECT pnl_pct FROM paper_trades WHERE status = 'closed' AND closed_at >= ?", (day,))
    open_n = db.q1("SELECT COUNT(*) AS n FROM paper_trades WHERE status = 'open'")["n"]
    if closed:
        pnls = [r["pnl_pct"] or 0 for r in closed]
        wins = sum(p > 0 for p in pnls)
        lines.append(f"Paper trades (fake money) closed today: {len(pnls)}, {wins} won, "
                     f"average {sum(pnls) / len(pnls):+.1f}% after fees. {open_n} still open.")
    else:
        lines.append(f"Paper trades (fake money): none closed today, {open_n} open.")
    alerts = db.q1("SELECT COUNT(*) AS n FROM alerts WHERE kind = 'verdict' AND sent_at >= ?", (day,))["n"]
    lines.append(f"Alerts sent today: {alerts}. Still practice only - no real money.")
    return "Coin Watch daily learning report", "\n".join(lines)
