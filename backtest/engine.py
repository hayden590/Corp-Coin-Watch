"""Backtesting on recorded history.

- Rows = feature snapshot at first sighting + recorded outcomes (+ 24h price path).
- Strategies are rule sets from config.yaml (`strategies:`); entries/exits are
  replayed on the price path, with realistic fees and slippage on both sides.
  Inside a candle where both stop and target are touched we assume the STOP hit
  first (pessimistic).
- Splits are always by TIME: train on older coins, test on newer unseen coins.
- Reports win rate, avg win/loss, max drawdown, EV per trade after costs, which
  signals actually predicted outcomes, the best global-fees thresholds, and an
  optional ML model (logistic regression / gradient boosting) predicting
  "hits +TP% before -SL%", a second one predicting "rugs within 24h", which
  signals come before rugs, and which hours of the day worked best.
- Warns loudly when results look too good to be true.
"""
from __future__ import annotations

import json
import logging
import math
import pickle
import statistics
from pathlib import Path
from typing import Any

from db import DB

log = logging.getLogger(__name__)

VERDICT_RANK = {"DANGER": 0, "UNCHECKED": 1, "UNCONFIRMED": 2, "VERIFIED": 3}
SIGNALS = [
    "backing", "connection", "chart_points", "text_points", "total", "liquidity_usd", "fdv", "age_minutes",
    "volume_h24", "top10_pct", "holder_count", "safety_warns", "smart_wallet_buys", "trusted_smart_buys",
    "endorsements_t1", "endorsements_t2", "telegram_channels", "global_fees_sol", "fees_to_volume",
    "official_confirmed", "narrative_flag", "first_time_poster", "chart_quality", "change_5m", "change_1h",
    "vol_ratio_5m", "volatility_5m", "ath_distance_pct", "buy_sell_ratio", "text_sentiment", "text_hype",
    "text_bot_like", "creator_confirmed", "hijack_flags", "tier1_followers",
]
RUG_CRASH = 0.9
NOT_SIGNALS = {"verdict_rank"}  # the verdict is our own rule output, not something to learn from twice


def signal_keys(rows: list[dict]) -> list[str]:
    """SIGNALS first, then every other numeric feature the snapshots recorded (new ones get
    picked up automatically as the bot records more)."""
    seen = {k for r in rows for k, v in r["features"].items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return [k for k in SIGNALS if k in seen] + sorted(seen - set(SIGNALS) - NOT_SIGNALS)


# --- data --------------------------------------------------------------------

def load_rows(db: DB) -> list[dict]:
    rows = []
    for s in db.q("SELECT * FROM feature_snapshots ORDER BY first_seen_at"):
        outs = {o["horizon"]: dict(o) for o in db.q("SELECT * FROM outcomes WHERE chain = ? AND address = ?",
                                                     (s["chain"], s["address"]))}
        if not outs:
            continue
        path = json.loads(outs["24h"]["path_json"]) if outs.get("24h") and outs["24h"].get("path_json") else None
        rows.append({"chain": s["chain"], "address": s["address"], "first_seen": s["first_seen_at"],
                     "entry_price": s["entry_price"], "verdict": s["verdict"],
                     "features": json.loads(s["features_json"]), "outcomes": outs, "path": path})
    return rows


def time_split(rows: list[dict], train_frac: float = 0.7) -> tuple[list[dict], list[dict]]:
    rows = sorted(rows, key=lambda r: r["first_seen"])
    k = int(len(rows) * train_frac)
    return rows[:k], rows[k:]


# --- rules & simulation -------------------------------------------------------

def matches(features: dict, verdict: str | None, rule: dict, ml_prob: float | None = None) -> bool:
    if VERDICT_RANK.get(verdict or "", -1) < VERDICT_RANK.get(rule.get("min_verdict", "UNCONFIRMED"), 2):
        return False
    checks = [("min_backing", "backing", ">="), ("min_total", "total", ">="), ("max_top10_pct", "top10_pct", "<="),
              ("min_liquidity_usd", "liquidity_usd", ">="), ("min_global_fees_sol", "global_fees_sol", ">="),
              ("min_fees_to_volume", "fees_to_volume", ">="), ("min_chart_quality", "chart_quality", ">="),
              ("min_trusted_smart_buys", "trusted_smart_buys", ">=")]
    for key, feat, op in checks:
        if key in rule and rule[key] is not None:
            v = features.get(feat)
            if v is None:
                return False
            if (op == ">=" and v < rule[key]) or (op == "<=" and v > rule[key]):
                return False
    if rule.get("min_ml_prob") is not None and (ml_prob is None or ml_prob < rule["min_ml_prob"]):
        return False
    return True


def simulate(row: dict, rule: dict, costs: dict) -> float | None:
    """Net return (fraction of stake) of one trade, or None if it can't be evaluated."""
    tp = rule.get("take_profit_pct", 50) / 100
    sl = rule.get("stop_loss_pct", 30) / 100
    hold = rule.get("max_hold_hours", 24) * 3600
    slip = costs.get("slippage_pct", 2) / 100
    fee = costs.get("fee_pct", 1) / 100
    entry = row.get("entry_price")
    gross = None
    if row.get("path") and entry:
        buy = entry * (1 + slip)
        for ts, hi, lo, close in row["path"]:
            if lo <= buy * (1 - sl):
                gross = -sl
                break
            if hi >= buy * (1 + tp):
                gross = tp
                break
            if ts - row["first_seen"] >= hold:
                gross = close / buy - 1
                break
        if gross is None:
            gross = row["path"][-1][3] / buy - 1
    else:
        o = pick_outcome(row["outcomes"], hold)
        if o is None:
            return None
        if o["max_drawdown"] is not None and o["max_drawdown"] <= -sl:
            gross = -sl  # pessimistic: without a path assume the stop came first
        elif o["max_gain"] is not None and o["max_gain"] >= tp:
            gross = tp
        else:
            gross = o["final_return"] or 0.0
        if o["rugged"]:
            gross = min(gross, -sl)
        gross -= slip
    net = (1 + gross) * (1 - slip) - 1 - 2 * fee
    return max(-1.0, net)


def pick_outcome(outcomes: dict, hold_s: float) -> dict | None:
    for h, secs in (("15m", 900), ("1h", 3600), ("6h", 21600), ("24h", 86400)):
        if secs >= hold_s and h in outcomes:
            return outcomes[h]
    return outcomes.get("24h") or (list(outcomes.values())[-1] if outcomes else None)


def label(row: dict, tp_pct: float, sl_pct: float) -> int | None:
    """1 = hit +TP% before -SL% within 24h, 0 = didn't, None = unknown."""
    r = simulate(row, {"take_profit_pct": tp_pct, "stop_loss_pct": sl_pct, "max_hold_hours": 24},
                 {"slippage_pct": 0, "fee_pct": 0})
    if r is None:
        return None
    return int(r >= tp_pct / 100 - 1e-9)


def rug_label(row: dict, tp_pct: float = 0, sl_pct: float = 0) -> int | None:
    """1 = rugged within 24h (liquidity pulled, honeypot, or price crashed 90%+ - on pump.fun a dev
    dump doesn't pull liquidity), 0 = didn't, None = not known yet."""
    outs = row["outcomes"].values()
    vals = [o.get("rugged") for o in outs if o.get("rugged") is not None]
    if any(vals) or any((o.get("max_drawdown") or 0) <= -RUG_CRASH for o in outs):
        return 1
    if not vals:
        return None
    return 0 if "24h" in row["outcomes"] else None


def metrics(pnls: list[float], stake_pct: float = 5.0) -> dict:
    if not pnls:
        return {"n": 0}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    equity = peak = 100.0
    max_dd = 0.0
    for p in pnls:
        equity += equity * stake_pct / 100 * p
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)
    return {"n": len(pnls), "win_rate": len(wins) / len(pnls),
            "avg_win": statistics.mean(wins) if wins else 0.0,
            "avg_loss": statistics.mean(losses) if losses else 0.0,
            "ev": statistics.mean(pnls), "max_drawdown_pct": round(max_dd, 2),
            "final_equity": round(equity, 2)}


def run_strategy(rows: list[dict], rule: dict, costs: dict, ml=None) -> list[float]:
    out = []
    for r in rows:
        prob = ml.predict(r["features"]) if ml else None
        if matches(r["features"], r["verdict"], rule, prob):
            p = simulate(r, rule, costs)
            if p is not None:
                out.append(p)
    return out


# --- signal attribution ----------------------------------------------------------

def attribution(rows: list[dict], tp: float, sl: float, min_n: int = 10, labeller=None) -> list[dict]:
    labeller = labeller or label
    labelled = [(r["features"], labeller(r, tp, sl)) for r in rows]
    labelled = [(f, y) for f, y in labelled if y is not None]
    out = []
    for key in signal_keys(rows):
        pairs = [(float(f[key]), y) for f, y in labelled if isinstance(f.get(key), (int, float))]
        if len(pairs) < min_n or len({x for x, _ in pairs}) < 2:
            continue
        xs, ys = zip(*pairs)
        corr = _corr(xs, ys)
        med = statistics.median(xs)
        hi = [y for x, y in pairs if x > med] or [y for x, y in pairs if x >= med]
        lo = [y for x, y in pairs if x <= med] if hi else []
        out.append({"signal": key, "n": len(pairs), "corr": round(corr, 3),
                    "hit_rate_high": round(sum(hi) / len(hi), 3) if hi else None,
                    "hit_rate_low": round(sum(lo) / len(lo), 3) if lo else None})
    return sorted(out, key=lambda d: -abs(d["corr"]))


def by_hour(rows: list[dict], tp: float, sl: float, block: int = 4, min_n: int = 10) -> list[dict]:
    """Hit rate and rug rate by time of day (UTC, in `block`-hour slots) - when buying worked best."""
    slots: dict[int, list[tuple[int | None, int | None]]] = {}
    for r in rows:
        h = r["features"].get("hour_utc")
        if isinstance(h, (int, float)):
            slots.setdefault(int(h) // block * block, []).append((label(r, tp, sl), rug_label(r)))
    out = []
    for start in sorted(slots):
        hits = [y for y, _ in slots[start] if y is not None]
        rugs = [y for _, y in slots[start] if y is not None]
        if len(hits) >= min_n:
            out.append({"hours": f"{start:02d}-{start + block:02d} UTC", "n": len(hits),
                        "hit_rate": round(sum(hits) / len(hits), 3),
                        "rug_rate": round(sum(rugs) / len(rugs), 3) if rugs else None})
    return out


def _corr(xs, ys) -> float:
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return 0.0 if sx == 0 or sy == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def best_threshold(train: list[dict], test: list[dict], base_rule: dict, key: str, feature: str,
                   costs: dict, min_n: int = 20) -> dict | None:
    """Sweep a threshold for one feature on TRAIN, report it on TEST."""
    vals = sorted({r["features"].get(feature) for r in train if isinstance(r["features"].get(feature), (int, float))})
    if len(vals) < 3:
        return None
    qs = [vals[int(i * (len(vals) - 1) / 10)] for i in range(10)]
    best = None
    for q in sorted(set(qs)):
        rule = {**base_rule, key: q}
        m = metrics(run_strategy(train, rule, costs))
        if m["n"] >= min_n and (best is None or m["ev"] > best["train"]["ev"]):
            best = {"threshold": q, "train": m}
    if best:
        best["test"] = metrics(run_strategy(test, {**base_rule, key: best["threshold"]}, costs))
    return best


# --- optional ML ---------------------------------------------------------------

class MLModel:
    def __init__(self, model, keys: list[str], medians: dict, kind: str):
        self.model, self.keys, self.medians, self.kind = model, keys, medians, kind
        self.test_auc: float | None = None   # measured on newer coins it never trained on
        self.n_train = 0

    def trustworthy(self, min_auc: float = 0.6, min_train: int = 150) -> bool:
        """Only let the model drive alerts once it has proven itself out-of-sample."""
        return (getattr(self, "test_auc", None) or 0) >= min_auc and getattr(self, "n_train", 0) >= min_train

    def vector(self, f: dict) -> list[float]:
        return [float(f[k]) if isinstance(f.get(k), (int, float)) else self.medians[k] for k in self.keys]

    def predict(self, f: dict) -> float:
        return float(self.model.predict_proba([self.vector(f)])[0][1])

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            pickle.dump(self, fh)

    @staticmethod
    def load(path: Path) -> "MLModel | None":
        # Only ever loads the file this program wrote itself into data/.
        if not path.exists():
            return None
        with open(path, "rb") as fh:
            return pickle.load(fh)


def train_ml(train: list[dict], test: list[dict], tp: float, sl: float,
             labeller=None) -> tuple[MLModel | None, dict]:
    try:
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import roc_auc_score
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        return None, {"skipped": "scikit-learn not installed"}
    labeller = labeller or label
    tr = [(r["features"], labeller(r, tp, sl)) for r in train]
    te = [(r["features"], labeller(r, tp, sl)) for r in test]
    tr = [(f, y) for f, y in tr if y is not None]
    te = [(f, y) for f, y in te if y is not None]
    if len(tr) < 50 or len(te) < 20 or len({y for _, y in tr}) < 2:
        return None, {"skipped": f"not enough labelled data (train {len(tr)}, test {len(te)}; need 50/20)"}
    keys = [k for k in signal_keys(train) if sum(isinstance(f.get(k), (int, float)) for f, _ in tr) >= len(tr) * 0.3]
    medians = {k: statistics.median([float(f[k]) for f, _ in tr if isinstance(f.get(k), (int, float))]) for k in keys}
    results, best = {}, None
    for kind, est in (("logistic_regression", make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000))),
                      ("gradient_boosting", GradientBoostingClassifier(max_depth=2, n_estimators=100))):
        m = MLModel(None, keys, medians, kind)
        est.fit([m.vector(f) for f, _ in tr], [y for _, y in tr])
        m.model = est
        probs = [m.predict(f) for f, _ in te]
        ys = [y for _, y in te]
        auc = roc_auc_score(ys, probs) if len(set(ys)) > 1 else None
        train_auc = roc_auc_score([y for _, y in tr], [m.predict(f) for f, _ in tr])
        results[kind] = {"test_auc": None if auc is None else round(auc, 3), "train_auc": round(train_auc, 3)}
        m.test_auc, m.n_train = auc, len(tr)
        if auc is not None and (best is None or auc > results[best.kind]["test_auc"]):
            best = m
    results["chosen"] = best.kind if best else None
    return best, results


# --- report --------------------------------------------------------------------

def overfit_warnings(train_m: dict, test_m: dict, n_strategies: int) -> list[str]:
    w = []
    if test_m.get("n", 0) < 30:
        w.append(f"only {test_m.get('n', 0)} test trades - far too few to trust")
    if test_m.get("n", 0) and test_m["win_rate"] > 0.7:
        w.append(f"test win rate {test_m['win_rate']:.0%} is suspiciously high")
    if test_m.get("n", 0) and test_m["ev"] > 0.5:
        w.append(f"test EV {test_m['ev']:+.0%}/trade is suspiciously high")
    if train_m.get("n", 0) and test_m.get("n", 0) and train_m["ev"] > 0 and train_m["ev"] > 2 * max(test_m["ev"], 0) + 0.05:
        w.append(f"train EV {train_m['ev']:+.1%} >> test EV {test_m['ev']:+.1%}: classic overfitting")
    if n_strategies > 5:
        w.append(f"{n_strategies} strategies compared - the best one is partly luck (multiple testing)")
    return w


def run(db: DB, cfg: dict, model_path: Path | None = None) -> dict:
    bt = cfg.get("backtest") or {}
    costs = {"fee_pct": bt.get("fee_pct", 1.0), "slippage_pct": bt.get("slippage_pct", 2.0)}
    tp, sl = bt.get("take_profit_pct", 50), bt.get("stop_loss_pct", 30)
    rows = load_rows(db)
    report: dict[str, Any] = {"rows": len(rows), "costs": costs, "tp": tp, "sl": sl}
    if len(rows) < 10:
        report["error"] = f"only {len(rows)} coins with recorded outcomes - let the bot collect history first"
        return report
    train, test = time_split(rows, bt.get("train_frac", 0.7))
    report["split"] = {"train": len(train), "test": len(test),
                       "train_until": train[-1]["first_seen"] if train else None}
    ml, ml_info = train_ml(train, test, tp, sl)
    report["ml"] = ml_info
    if ml and model_path:
        ml.save(model_path)
    rug, rug_info = train_ml(train, test, tp, sl, labeller=rug_label)
    report["rug_ml"] = rug_info
    if rug and model_path:
        rug.save(rug_model_path(model_path))
    strategies = cfg.get("strategies") or []
    report["strategies"] = []
    for s in strategies:
        use_ml = ml if s.get("min_ml_prob") is not None else None
        tr_m = metrics(run_strategy(train, s, costs, use_ml), bt.get("stake_pct", 5))
        te_m = metrics(run_strategy(test, s, costs, use_ml), bt.get("stake_pct", 5))
        report["strategies"].append({"name": s.get("name"), "train": tr_m, "test": te_m,
                                     "warnings": overfit_warnings(tr_m, te_m, len(strategies))})
    report["signals"] = attribution(train + test, tp, sl)
    report["rug_signals"] = attribution(train + test, tp, sl, labeller=rug_label)
    report["hours"] = by_hour(train + test, tp, sl)
    base = strategies[0] if strategies else {"min_verdict": "UNCONFIRMED"}
    report["fee_thresholds"] = {
        "min_global_fees_sol": best_threshold(train, test, base, "min_global_fees_sol", "global_fees_sol", costs),
        "min_fees_to_volume": best_threshold(train, test, base, "min_fees_to_volume", "fees_to_volume", costs),
    }
    return report


def rug_model_path(model_path: Path) -> Path:
    return Path(model_path).with_name("rug_model.pkl")


def similar_stats(db: DB, verdict: str, backing: float, chain: str, cfg: dict, min_n: int = 10) -> dict:
    bt = cfg.get("backtest") or {}
    tp, sl = bt.get("take_profit_pct", 50), bt.get("stop_loss_pct", 30)
    rows = [r for r in load_rows(db) if r["verdict"] == verdict and r["chain"] == chain
            and abs((r["features"].get("backing") or 0) - backing) <= 1.0]
    labels = [y for y in (label(r, tp, sl) for r in rows) if y is not None]
    if len(labels) < min_n:
        return {"n": len(labels), "text": f"not enough history yet (n={len(labels)})"}
    rate = sum(labels) / len(labels)
    return {"n": len(labels), "rate": rate,
            "text": f"{rate:.0%} hit +{tp}% before -{sl}% (n={len(labels)} similar {verdict} coins on {chain})"}


def format_report(rep: dict) -> str:
    L = ["BACKTEST (split by time: train on older coins, test on newer unseen coins)", ""]
    if rep.get("error"):
        return "\n".join(L + [rep["error"]])
    c = rep["costs"]
    L.append(f"{rep['rows']} coins with outcomes | train {rep['split']['train']} / test {rep['split']['test']} | "
             f"costs: {c['fee_pct']}% fee + {c['slippage_pct']}% slippage per side | label: +{rep['tp']}% before -{rep['sl']}%")
    for s in rep["strategies"]:
        L.append("")
        L.append(f"Strategy '{s['name']}':")
        for part in ("train", "test"):
            m = s[part]
            if not m.get("n"):
                L.append(f"  {part:<5} no trades")
                continue
            L.append(f"  {part:<5} n={m['n']:<4} win {m['win_rate']:.0%}  avg win {m['avg_win']:+.1%}  avg loss "
                     f"{m['avg_loss']:+.1%}  EV/trade {m['ev']:+.2%}  max DD {m['max_drawdown_pct']:.1f}%")
        for w in s["warnings"]:
            L.append(f"  !!! OVERFITTING WARNING: {w}")
    L += ["", "Which signals predicted outcomes (correlation with hitting the target):"]
    for sig in rep["signals"][:15]:
        L.append(f"  {sig['signal']:<20} corr {sig['corr']:+.3f}  hit-rate high {sig['hit_rate_high']}  "
                 f"low {sig['hit_rate_low']}  (n={sig['n']})")
    if not rep["signals"]:
        L.append("  not enough labelled data yet")
    L += ["", "Rug warning signs (correlation with rugging within 24h; + = more of it, more rugs):"]
    for sig in rep.get("rug_signals", [])[:12]:
        L.append(f"  {sig['signal']:<20} corr {sig['corr']:+.3f}  rug-rate high {sig['hit_rate_high']}  "
                 f"low {sig['hit_rate_low']}  (n={sig['n']})")
    if not rep.get("rug_signals"):
        L.append("  not enough rug outcomes yet")
    L += ["", "Time of day (UTC) - how often coins first seen then hit the target / rugged:"]
    for h in rep.get("hours", []):
        rug = f"{h['rug_rate']:.0%}" if h["rug_rate"] is not None else "?"
        L.append(f"  {h['hours']}  hit {h['hit_rate']:.0%}  rug {rug}  (n={h['n']})")
    if not rep.get("hours"):
        L.append("  not enough data per time slot yet")
    L += ["", "Global fees thresholds (chosen on train, checked on test):"]
    for k, v in rep["fee_thresholds"].items():
        if not v:
            L.append(f"  {k}: not enough data")
        else:
            te = v["test"]
            L.append(f"  {k} >= {v['threshold']:.4g}: train EV {v['train']['ev']:+.2%} (n={v['train']['n']}), "
                     f"test EV {te.get('ev', 0):+.2%} (n={te.get('n', 0)})")
    L += ["", f"ML (hits target): {json.dumps(rep['ml'])}",
          f"ML (rug spotter): {json.dumps(rep.get('rug_ml'))}"]
    return "\n".join(L)
