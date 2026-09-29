"""Chart analysis: OHLCV from GeckoTerminal (free, no key), fallback Birdeye
(free tier, BIRDEYE_API_KEY). Candles are cached in memory per timeframe.

Features: price change per timeframe, volume vs rolling average, buy/sell
ratio, volatility, higher-highs / lower-highs structure, distance from ATH,
time since launch, and liquidity / holder-count / top-holder trends from our
own market snapshots. Exit-warning flags are computed from the same history.
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field

from db import DB
from net import Http
from sources.dex_source import MarketInfo

log = logging.getLogger(__name__)

GT_BASE = "https://api.geckoterminal.com/api/v2"
GT_NETWORKS = {"solana": "solana", "ethereum": "eth", "base": "base", "bsc": "bsc",
               "arbitrum": "arbitrum", "polygon": "polygon_pos"}
BIRDEYE_URL = "https://public-api.birdeye.so/defi/ohlcv"
TIMEFRAMES = {"1m": ("minute", 1, 60), "5m": ("minute", 5, 300), "15m": ("minute", 15, 900), "1h": ("hour", 1, 3600)}

Candle = tuple[float, float, float, float, float, float]  # ts, open, high, low, close, volume (oldest first)


@dataclass
class ChartReport:
    features: dict = field(default_factory=dict)
    quality: float | None = None       # 0..1
    entry: str = "unknown"
    summary: str = "no chart data"


# --- pure feature functions --------------------------------------------------

def pct_change(c: list[Candle], n: int) -> float | None:
    if len(c) <= n or c[-1 - n][4] <= 0:
        return None
    return (c[-1][4] / c[-1 - n][4] - 1) * 100


def vol_ratio(c: list[Candle], window: int = 12) -> float | None:
    if len(c) < 3:
        return None
    prev = [x[5] for x in c[-1 - window:-1]]
    avg = sum(prev) / len(prev) if prev else 0
    return c[-1][5] / avg if avg > 0 else None


def volatility(c: list[Candle], window: int = 24) -> float | None:
    closes = [x[4] for x in c[-window - 1:] if x[4] > 0]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    if len(rets) < 3:
        return None
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * 100


def structure(c: list[Candle]) -> str:
    """Compare the last two swing highs and lows."""
    highs = [c[i][2] for i in range(1, len(c) - 1) if c[i][2] > c[i - 1][2] and c[i][2] >= c[i + 1][2]]
    lows = [c[i][3] for i in range(1, len(c) - 1) if c[i][3] < c[i - 1][3] and c[i][3] <= c[i + 1][3]]
    if len(highs) < 2 or len(lows) < 2:
        return "unclear"
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return "higher_highs"
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return "lower_highs"
    return "mixed"


def ath_distance(c: list[Candle]) -> float | None:
    if not c:
        return None
    ath = max(x[2] for x in c)
    return (c[-1][4] / ath - 1) * 100 if ath > 0 else None


def trend(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    if len(vals) < 2 or not vals[0]:
        return None
    return (vals[-1] / vals[0] - 1) * 100


def score_chart(f: dict) -> tuple[float, str]:
    q = 0.5
    st = f.get("structure")
    q += 0.15 if st == "higher_highs" else -0.2 if st == "lower_highs" else 0
    bsr = f.get("buy_sell_ratio")
    if bsr is not None:
        q += 0.1 if bsr > 1.2 else -0.1 if bsr < 0.8 else 0
    if (f.get("vol_ratio_5m") or 0) > 1.5:
        q += 0.05
    ch1h, ath = f.get("change_1h"), f.get("ath_distance_pct")
    if (ch1h or 0) > 150:
        q -= 0.15
    if ath is not None and ath < -60:
        q -= 0.15
    if (f.get("liquidity_trend_pct") or 0) < -20:
        q -= 0.2
    q = max(0.0, min(1.0, q))

    if (ch1h or 0) > 150 and ath is not None and ath > -10:
        entry = "overextended - chasing a pump"
    elif st == "lower_highs" and (ch1h or 0) < -20:
        entry = "dumping - lower highs"
    elif st == "higher_highs" and ath is not None and -35 <= ath <= -10:
        entry = "pullback in an uptrend"
    elif ath is not None and ath > -10:
        entry = "near highs"
    else:
        entry = "neutral"
    return round(q, 2), entry


def summarise(f: dict) -> str:
    parts = []
    for key, label in (("change_1h", "1h"), ("change_15m", "15m"), ("change_5m", "5m")):
        if f.get(key) is not None:
            parts.append(f"{label} {f[key]:+.0f}%")
    if f.get("vol_ratio_5m") is not None:
        parts.append(f"vol x{f['vol_ratio_5m']:.1f}")
    if f.get("buy_sell_ratio") is not None:
        parts.append(f"buys/sells {f['buy_sell_ratio']:.2f}")
    if f.get("structure") not in (None, "unclear"):
        parts.append(f["structure"].replace("_", " "))
    if f.get("ath_distance_pct") is not None:
        parts.append(f"{-f['ath_distance_pct']:.0f}% below ATH" if f["ath_distance_pct"] < -1 else "at ATH")
    return " | ".join(parts) or "no chart data"


def exit_flags(snaps: list[dict], cfg: dict, deployer_sells: int = 0, smart_exits: list[dict] | None = None) -> list[tuple[str, str]]:
    """snaps: market_snapshots rows (oldest first) since the coin was alerted."""
    flags: list[tuple[str, str]] = []
    if deployer_sells:
        flags.append(("dev_selling", f"deployer wallet sold ({deployer_sells} sell tx)"))
    if smart_exits:
        who = ", ".join(sorted({e.get("label") or e["wallet"][:8] for e in smart_exits})[:3])
        flags.append(("smart_wallets_exiting", f"smart wallet(s) sold: {who}"))
    if len(snaps) < 2:
        return flags
    last = snaps[-1]
    liqs = [s["liquidity_usd"] for s in snaps if s.get("liquidity_usd")]
    if liqs and last.get("liquidity_usd") is not None and last["liquidity_usd"] < max(liqs) * (1 - cfg.get("liquidity_drop_pct", 50) / 100):
        flags.append(("liquidity_removed", f"liquidity ${last['liquidity_usd']:,.0f}, down from ${max(liqs):,.0f}"))
    t10 = [s["top10_pct"] for s in snaps if s.get("top10_pct") is not None]
    if len(t10) >= 2 and max(t10) - t10[-1] >= cfg.get("top10_drop_points", 5):
        flags.append(("top10_selling", f"top-10 holders' share fell {max(t10):.1f}% -> {t10[-1]:.1f}%"))
    hc = [s["holder_count"] for s in snaps if s.get("holder_count")]
    if len(hc) >= 2 and hc[-1] < max(hc) * (1 - cfg.get("holder_drop_pct", 10) / 100):
        flags.append(("holder_count_dropping", f"holders {hc[-1]} (peak {max(hc)})"))
    prices = [s["price_usd"] for s in snaps if s.get("price_usd")]
    buys = [s["buys_h1"] for s in snaps if s.get("buys_h1") is not None]
    if prices and buys and len(buys) >= 2 and prices[-1] >= 0.9 * max(prices) and prices[-1] > prices[0] \
            and buys[-1] < max(buys) * (1 - cfg.get("buy_fade_pct", 40) / 100):
        flags.append(("buy_volume_fading", f"price near high but buys/h fell to {buys[-1]} from {max(buys)}"))
    return flags


# --- fetching ----------------------------------------------------------------

def parse_gt(data) -> list[Candle] | None:
    try:
        rows = data["data"]["attributes"]["ohlcv_list"]
    except (KeyError, TypeError):
        return None
    out = []
    for r in rows or []:
        try:
            out.append(tuple(float(v) for v in r[:6]))
        except (TypeError, ValueError):
            continue
    return sorted(out) if out else None


def parse_birdeye(data) -> list[Candle] | None:
    items = ((data or {}).get("data") or {}).get("items") if isinstance(data, dict) else None
    out = []
    for i in items or []:
        try:
            out.append((float(i["unixTime"]), float(i["o"]), float(i["h"]), float(i["l"]), float(i["c"]), float(i.get("v", 0))))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out) if out else None


class Charts:
    def __init__(self, db: DB, http: Http, cfg: dict, birdeye_key: str = ""):
        self.db = db
        self.http = http
        self.cfg = cfg.get("charts") or {}
        self.birdeye_key = birdeye_key
        self._cache: dict[tuple, tuple[float, list[Candle] | None]] = {}

    async def candles(self, chain: str, pool: str | None, token: str, tf: str, limit: int = 100) -> list[Candle] | None:
        key = (chain, pool, token, tf, limit)
        unit, agg, secs = TIMEFRAMES[tf]
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < secs:
            return hit[1]
        out = None
        net = GT_NETWORKS.get(chain)
        if net and pool:
            r = await self.http.get_json(f"{GT_BASE}/networks/{net}/pools/{pool}/ohlcv/{unit}",
                                         params={"aggregate": agg, "limit": limit, "currency": "usd"})
            out = parse_gt(r.data) if r.ok else None
        if out is None and self.birdeye_key:
            now = int(time.time())
            r = await self.http.get_json(BIRDEYE_URL, params={"address": token, "type": tf.upper() if tf == "1h" else tf,
                                                              "time_from": now - secs * limit, "time_to": now},
                                         headers={"X-API-KEY": self.birdeye_key, "x-chain": chain}, log_name="birdeye-ohlcv")
            out = parse_birdeye(r.data) if r.ok else None
        self._cache[key] = (time.time(), out)
        return out

    def snapshot_market(self, m: MarketInfo, holder_count: int | None, top10: float | None) -> None:
        self.db.x("""INSERT INTO market_snapshots (chain, address, taken_at, price_usd, liquidity_usd, volume_h1,
                     buys_h1, sells_h1, holder_count, top10_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (m.chain, m.address, time.time(), m.price_usd, m.liquidity_usd, m.volume_h1, m.buys_h1, m.sells_h1,
                   holder_count, top10))

    def snapshots(self, chain: str, address: str, since: float) -> list[dict]:
        return [dict(r) for r in self.db.q("""SELECT * FROM market_snapshots WHERE chain = ? AND address = ?
                                              AND taken_at >= ? ORDER BY taken_at""", (chain, address, since))]

    async def analyse(self, m: MarketInfo) -> ChartReport:
        c1 = await self.candles(m.chain, m.pair_address, m.address, "1m")
        c5 = await self.candles(m.chain, m.pair_address, m.address, "5m")
        c15 = await self.candles(m.chain, m.pair_address, m.address, "15m")
        c1h = await self.candles(m.chain, m.pair_address, m.address, "1h")
        f: dict = {}
        if c1:
            f["change_1m"] = pct_change(c1, 1)
        if c5:
            f.update(change_5m=pct_change(c5, 1), vol_ratio_5m=vol_ratio(c5), volatility_5m=volatility(c5),
                     structure=structure(c5[-36:]))
        if c15:
            f["change_15m"] = pct_change(c15, 1)
        f["change_1h"] = (pct_change(c1h, 1) if c1h and len(c1h) > 1 else None) or m.price_change_h1
        longest = c1h if c1h and len(c1h) > 3 else c15 or c5
        f["ath_distance_pct"] = ath_distance(longest) if longest else None
        if m.buys_h1 is not None and m.sells_h1:
            f["buy_sell_ratio"] = round(m.buys_h1 / m.sells_h1, 2)
        f["minutes_since_launch"] = round(m.age_minutes, 1) if m.age_minutes is not None else None
        snaps = self.snapshots(m.chain, m.address, time.time() - 2 * 3600)
        f["liquidity_trend_pct"] = trend([s["liquidity_usd"] for s in snaps])
        f["holder_trend_pct"] = trend([s["holder_count"] for s in snaps])
        f["top10_trend_pct"] = trend([s["top10_pct"] for s in snaps])
        f = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in f.items()}
        rep = ChartReport(features=f)
        if not any(v is not None for k, v in f.items() if k not in ("minutes_since_launch",)):
            return rep
        rep.quality, rep.entry = score_chart(f)
        rep.summary = summarise(f)
        self.db.x("INSERT INTO chart_snapshots (chain, address, taken_at, features_json) VALUES (?, ?, ?, ?)",
                  (m.chain, m.address, time.time(), json.dumps(f)))
        return rep

    async def path_since(self, chain: str, pool: str | None, token: str, since: float) -> list[Candle] | None:
        """Candles covering [since, now] for outcome tracking (5m up to ~8h, else 15m)."""
        age = time.time() - since
        tf = "5m" if age <= 8 * 3600 else "15m"
        secs = TIMEFRAMES[tf][2]
        c = await self.candles(chain, pool, token, tf, limit=min(1000, int(age / secs) + 2))
        return [x for x in c if x[0] + secs > since] if c else None
