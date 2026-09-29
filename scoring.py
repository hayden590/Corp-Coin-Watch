"""Scores and verdicts.

Backing score    = signal-account endorsements (tier 1 counts ONLY for direct
                   engagement with the CA) + distinct Telegram channels + smart
                   wallet buys weighted by each wallet's track record (+ a little
                   for ordinary X posters, minus blacklisted posters).
Connection score = tier-1/2 follows and interactions with the confirmed creator,
                   recent interactions >> old follows. Zero if the creator isn't
                   confirmed or the account looks bought/hijacked.
Chart / text     = small adjustments.
All weights live in config.yaml -> scoring.

Verdicts:
  DANGER      deleted source tweet, recent account changes, honeypot / failed
              safety, insider dump flag, hijacked-account flag
  UNCHECKED   a critical safety check couldn't be answered
  VERIFIED    official website confirms CA + source tweet persisted + safety OK
  UNCONFIRMED safety OK, no official confirmation
Nothing overrides DANGER. Connection/text/chart scores alone never cause an alert.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from safety import FAIL, PASS, UNKNOWN, WARN, SafetyReport

DANGER = "DANGER"
VERIFIED = "VERIFIED"
UNCONFIRMED = "UNCONFIRMED"
UNCHECKED = "UNCHECKED"

VERDICT_RANK = {DANGER: 0, UNCHECKED: 1, UNCONFIRMED: 2, VERIFIED: 3}


@dataclass
class Verdict:
    label: str
    reasons: list[str] = field(default_factory=list)

    @property
    def is_danger(self) -> bool:
        return self.label == DANGER


@dataclass
class Assessment:
    """Everything known about one CA at one moment. Later-phase fields are optional."""
    chain: str
    address: str
    safety: SafetyReport
    market: Any = None                 # MarketInfo
    legit: Any = None                  # verify.LegitReport
    fees: Any = None                   # fees.FeeResult
    connections: Any = None            # graph.ConnectionReport
    chart: Any = None                  # charts.ChartReport
    text: Any = None                   # text_analysis.TextResult
    smart_buys: list[dict] = field(default_factory=list)
    dump_flags: list[str] = field(default_factory=list)
    exit_flags: list[tuple[str, str]] = field(default_factory=list)
    backing: float = 0.0
    backing_breakdown: list[tuple[str, float]] = field(default_factory=list)
    connection: float = 0.0
    connection_breakdown: list[tuple[str, float]] = field(default_factory=list)
    chart_points: float = 0.0
    text_points: float = 0.0
    penalties: list[tuple[str, float]] = field(default_factory=list)
    similar: dict | None = None
    verdict: Verdict | None = None
    ml_prob: float | None = None       # trained model's chance of hitting the target (None = no trusted model)
    x_mentions: int | None = None      # recent X posts mentioning this CA
    rug_prob: float | None = None      # rug model's chance this coin rugs (None = no trusted model)
    comments: int | None = None        # comments / theses on the token's own page (pump.fun, Fomo)
    seen_at: float | None = None       # first sighting (for time-of-day features)

    @property
    def total(self) -> float:
        return round(self.backing + self.connection + self.chart_points + self.text_points
                     + sum(p for _, p in self.penalties), 2)

    def features(self) -> dict:
        """Flat numeric features for backtest snapshots."""
        m = self.market
        f = {
            "backing": self.backing, "connection": self.connection, "chart_points": self.chart_points,
            "text_points": self.text_points, "total": self.total,
            "verdict_rank": VERDICT_RANK.get(self.verdict.label if self.verdict else "", None),
            "liquidity_usd": getattr(m, "liquidity_usd", None), "fdv": getattr(m, "fdv", None),
            "age_minutes": getattr(m, "age_minutes", None), "volume_h24": getattr(m, "volume_h24", None),
            "top10_pct": self.safety.top10_pct, "holder_count": self.safety.holder_count,
            "safety_warns": sum(c.status == WARN for c in self.safety.checks),
            "smart_wallet_buys": len(self.smart_buys),
            "trusted_smart_buys": sum(1 for b in self.smart_buys if b["record"].trusted),
            "endorsements_t1": sum(1 for l, _ in self.backing_breakdown if l.startswith("tier-1")),
            "endorsements_t2": sum(1 for l, _ in self.backing_breakdown if l.startswith("tier-2")),
            "telegram_channels": sum(1 for l, _ in self.backing_breakdown if l.startswith("telegram")),
            "x_mentions": self.x_mentions, "comments": self.comments,
        }
        # Rug warning signs, one number per safety check: 0 pass, 1 warn, 2 fail (missing = unknown).
        for c in self.safety.checks:
            if c.status in SAFETY_LEVEL:
                f[f"safety_{c.name}"] = SAFETY_LEVEL[c.status]
        # Market activity from DexScreener - known for every coin, not only the deeply analysed ones.
        if m is not None:
            f.update(market_features(m))
        # When it was seen (UTC), so the backtest can show which hours / days work best.
        t = time.gmtime(self.seen_at or time.time())
        f["hour_utc"], f["weekday"] = t.tm_hour, t.tm_wday
        if self.fees is not None:
            f["global_fees_sol"] = self.fees.global_fees_sol
            f["fees_to_volume"] = self.fees.fees_to_volume
        if self.legit is not None:
            f["official_confirmed"] = int(self.legit.official_confirmed)
            nar = self.legit.get("narrative")
            f["narrative_flag"] = int(bool(nar and nar.status == WARN))
            ftp = self.legit.get("first_time_poster")
            f["first_time_poster"] = int(bool(ftp and ftp.status == WARN))
        if self.chart is not None:
            f["chart_quality"] = self.chart.quality
            for k, v in self.chart.features.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    f[k] = v
            if isinstance(self.chart.features.get("structure"), str):
                f["higher_highs"] = int(self.chart.features["structure"] == "higher_highs")
                f["lower_highs"] = int(self.chart.features["structure"] == "lower_highs")
        if self.text is not None:
            f["text_sentiment"] = self.text.sentiment
            f["text_hype"] = self.text.hype_vs_substance
            f["text_bot_like"] = int(self.text.bot_like)
            f["text_posts"] = self.text.n_texts
            f["text_duplicate_ratio"] = self.text.duplicate_ratio
            f["text_red_flags"] = len(self.text.red_flags)
        if self.connections is not None:
            f["creator_confirmed"] = int(self.connections.creator is not None)
            f["hijack_flags"] = len(self.connections.hijack_flags)
            f["tier1_followers"] = len(self.connections.tier1_followers)
            f["deployer_flags"] = len(self.connections.deployer_flags)
        return f


SAFETY_LEVEL = {PASS: 0, WARN: 1, FAIL: 2}


def _ratio(a, b) -> float | None:
    return round(a / b, 4) if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b > 0 else None


def market_features(m) -> dict:
    """Price moves, buy/sell pressure and money flow from DexScreener's pair data."""
    g = lambda k: getattr(m, k, None)  # noqa: E731
    f = {
        "price_change_m5": g("price_change_m5"), "price_change_h1": g("price_change_h1"),
        "price_change_h6": g("price_change_h6"), "volume_m5": g("volume_m5"), "volume_h1": g("volume_h1"),
        "buys_m5": g("buys_m5"), "sells_m5": g("sells_m5"), "buys_h1": g("buys_h1"), "sells_h1": g("sells_h1"),
        "buy_sell_m5": _ratio(g("buys_m5"), g("sells_m5")),
        "buy_sell_h1": _ratio(g("buys_h1"), g("sells_h1")),
        "buy_sell_h24": _ratio(g("buys_h24"), g("sells_h24")),
        "volume_h1_to_liquidity": _ratio(g("volume_h1"), g("liquidity_usd")),
        "fdv_to_liquidity": _ratio(g("fdv"), g("liquidity_usd")),
    }
    return {k: v for k, v in f.items() if v is not None}


# --- component scores ---------------------------------------------------------

def backing_score(endorsements: list[dict], channels: list[dict], x_posters: int, blacklisted: list[str],
                  smart_buys: list[dict], fomo_theses: int, w: dict) -> tuple[float, list[tuple[str, float]]]:
    parts: list[tuple[str, float]] = []
    seen_users = set()
    for e in endorsements:
        if e["user_id"] in seen_users:
            continue
        seen_users.add(e["user_id"])
        if e["tier"] == 1:
            pts = w.get("tier1_weight", 3.0) * e["weight"]
        elif e.get("promoted"):
            pts = w.get("promoted_weight", 1.0)
        else:
            pts = w.get("tier2_weight", 1.5) * e["weight"]
        parts.append((f"tier-{e['tier']} @{e['handle'] or e['user_id']} ({e['kind']})", round(pts, 2)))
    for c in channels:
        parts.append((f"telegram {c['label']}", round(w.get("channel_weight", 1.0) * c["weight"], 2)))
    if x_posters:
        parts.append((f"{x_posters} other X poster(s)",
                      round(min(w.get("x_poster_cap", 1.0), w.get("x_poster_weight", 0.2) * x_posters), 2)))
    for h in blacklisted:
        parts.append((f"blacklisted poster @{h}", w.get("blacklisted_penalty", -1.0)))
    for b in smart_buys:
        rec = b["record"]
        if not rec.trusted:
            parts.append((f"wallet {b['label'] or b['wallet'][:6]} bought (not enough history, 0)", 0.0))
            continue
        pts = w.get("smart_wallet_weight", 1.0) * rec.multiplier * (0.5 if b["sold"] else 1.0)
        parts.append((f"wallet {b['label'] or b['wallet'][:6]} bought (win {rec.win_rate:.0%}"
                      f"{', already sold' if b['sold'] else ''})", round(pts, 2)))
    if fomo_theses:
        parts.append((f"{fomo_theses} Fomo thesis(es)", round(min(0.5, w.get("fomo_thesis_weight", 0.1) * fomo_theses), 2)))
    return round(sum(p for _, p in parts), 2), parts


def connection_score(conn, w: dict) -> tuple[float, list[tuple[str, float]]]:
    if conn is None or conn.creator is None:
        return 0.0, []
    if conn.hijack_flags:
        return 0.0, [("bonus cancelled: possible bought/hijacked account", 0.0)]
    parts: list[tuple[str, float]] = []
    tau = float(w.get("recency_days", 30))
    for i in conn.interactions:
        base = w.get("tier1_interaction", 2.0) if i["tier"] == 1 else w.get("tier2_interaction", 0.5)
        pts = base * math.exp(-i["days_ago"] / tau)
        parts.append((f"tier-{i['tier']} @{i['handle']} {i['kind']} {i['days_ago']:.0f}d ago", round(pts, 2)))
    for h in conn.tier1_followers:
        parts.append((f"followed by tier-1 @{h}", w.get("tier1_follow", 0.5)))
    if conn.tier2_followers:
        parts.append((f"followed by {conn.tier2_followers} tier-2",
                      round(min(w.get("tier2_follow_cap", 1.0), w.get("tier2_follow", 0.1) * conn.tier2_followers), 2)))
    fq = conn.follower_quality
    if fq and fq["sample"] >= 20 and (fq["no_pfp_pct"] + fq["zero_tweets_pct"]) / 2 >= 50:
        parts.append(("low-quality followers", w.get("fake_followers_penalty", -0.5)))
    total = sum(p for _, p in parts) * w.get("connection_weight", 1.0)
    return round(total, 2), parts


def chart_points(chart, w: dict) -> float:
    if chart is None or chart.quality is None:
        return 0.0
    return round((chart.quality - 0.5) * 2 * w.get("chart_weight", 1.0), 2)


def text_points(text, w: dict) -> float:
    if text is None or not text.n_texts:
        return 0.0
    tw = w.get("text_weight", 0.5)
    pts = 0.0
    if text.sentiment is not None:
        hype = 0.5 if text.hype_vs_substance is None else text.hype_vs_substance
        pts += text.sentiment * (1 - hype) * tw
    if text.bot_like:
        pts -= 0.5 * tw
    if text.red_flags:
        pts -= 0.25 * tw
    return round(max(-tw, min(tw, pts)), 2)


# --- verdict ------------------------------------------------------------------

def verdict(safety: SafetyReport, a: Assessment | None = None, hijack_is_danger: bool = True) -> Verdict:
    reasons = [f"{c.name}: {c.detail}" for c in safety.checks if c.status == FAIL]
    if a is not None:
        if a.legit is not None:
            reasons += [f"{c.name}: {c.detail}" for c in a.legit.checks
                        if c.status == FAIL and c.name in ("tweet_persists", "account_integrity")]
        reasons += [f"insider dump: {d}" for d in a.dump_flags]
        if hijack_is_danger and a.connections is not None:
            reasons += [f"hijacked-account flag: {h}" for h in a.connections.hijack_flags]
    if reasons:
        return Verdict(DANGER, reasons)
    if safety.overall == UNKNOWN:
        return Verdict(UNCHECKED, [f"{c.name}: {c.detail}" for c in safety.checks if c.status == UNKNOWN])
    if a is not None and a.legit is not None and a.legit.official_confirmed and a.legit.persisted is True \
            and safety.overall in (PASS, WARN):
        return Verdict(VERIFIED, ["official website lists this CA", "source tweet still up", "safety checks passed"])
    return Verdict(UNCONFIRMED, ["safety checks passed; no official confirmation"])


def score(a: Assessment, cfg: dict, backing_inputs: dict) -> Assessment:
    w = cfg.get("scoring") or {}
    a.backing, a.backing_breakdown = backing_score(w=w, smart_buys=a.smart_buys, **backing_inputs)
    a.connection, a.connection_breakdown = connection_score(a.connections, w)
    a.chart_points = chart_points(a.chart, w)
    a.text_points = text_points(a.text, w)
    a.penalties = []
    if a.legit is not None:
        nar = a.legit.get("narrative")
        if nar and nar.status == WARN:
            a.penalties.append(("narrative coin", w.get("narrative_penalty", -1.0)))
    if a.connections is not None and a.connections.deployer_flags:
        a.penalties.append(("deployer history", w.get("deployer_penalty", -1.0)))
    a.verdict = verdict(a.safety, a, bool((cfg.get("graph") or {}).get("hijack_is_danger", True)))
    return a


def should_alert(a: Assessment, sources: set[str], cfg: dict) -> tuple[bool, str]:
    """Only backing (people / channels / wallets actually pushing it) or an official
    confirmation can trigger an alert. Connection/text/chart scores never do."""
    acfg = cfg.get("alerts") or {}
    label = a.verdict.label
    if label in (DANGER, UNCHECKED):
        allowed = set(acfg.get("danger_alert_sources") or [])
        return (bool(sources & allowed), "unsafe coin being pushed" if sources & allowed else "unsafe, feed-only")
    if a.fees is not None and a.fees.status == "ok" and a.fees.low_activity:
        return False, f"low activity ({a.fees.global_fees_sol} SOL fees < min)"
    if label == VERIFIED:
        return True, "verified"
    if "manual" in sources:
        return True, "manual check"
    if a.backing >= float(acfg.get("min_backing_to_alert", 1.0)):
        return True, f"backing {a.backing}"
    # AI pick: a model that has proven itself on unseen coins rates this highly. It still needs at
    # least one hard signal (a trusted wallet buying, some backing, or a decent chart) - chatter or
    # connections alone never trigger an alert.
    prob_min = float(acfg.get("ai_pick_min_prob", 0.65))
    hard_signal = (a.backing > 0 or any(b["record"].trusted for b in a.smart_buys)
                   or (a.chart is not None and (a.chart.quality or 0) >= 0.6))
    rug_ok = a.rug_prob is None or a.rug_prob < float(acfg.get("ai_pick_max_rug_prob", 0.4))
    if a.ml_prob is not None and a.ml_prob >= prob_min and hard_signal and rug_ok:
        return True, f"AI pick ({a.ml_prob:.0%})"
    return False, f"backing {a.backing} < {acfg.get('min_backing_to_alert', 1.0)}"
