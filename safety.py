"""Safety checks: DexScreener market data + RugCheck (Solana) or GoPlus (EVM).

Every check is pass / warn / fail / unknown. An API failure or missing field
is "unknown" - never a crash and never silently a pass.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from net import Http
from sources.dex_source import MarketInfo

log = logging.getLogger(__name__)

PASS, WARN, FAIL, UNKNOWN = "pass", "warn", "fail", "unknown"

RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
GOPLUS_URL = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}"

GOPLUS_CHAIN_IDS = {
    "ethereum": "1",
    "bsc": "56",
    "base": "8453",
    "arbitrum": "42161",
    "polygon": "137",
    "optimism": "10",
    "avalanche": "43114",
}

# DexScreener dexIds for pump.fun-style bonding curves: there is no LP to pull yet.
BONDING_CURVE_DEXES = {"pumpfun", "moonshot", "letsbonk", "bonk"}

BURN_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0xdead000000000000000042069420694206942069",
}

# Checks whose "unknown" makes the whole report unknown (we can't call it safe).
CRITICAL = {"liquidity", "top10_holders", "mint_authority", "freeze_authority", "honeypot", "taxes"}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


@dataclass
class SafetyReport:
    chain: str
    address: str
    checks: list[Check] = field(default_factory=list)
    top10_pct: float | None = None
    market: dict[str, Any] | None = None
    deployer: str | None = None
    holder_count: int | None = None

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(name, status, detail))

    def get(self, name: str) -> Check | None:
        return next((c for c in self.checks if c.name == name), None)

    @property
    def overall(self) -> str:
        statuses = {c.name: c.status for c in self.checks}
        if FAIL in statuses.values():
            return FAIL
        if not statuses or any(s == UNKNOWN for n, s in statuses.items() if n in CRITICAL):
            return UNKNOWN
        if any(s in (WARN, UNKNOWN) for s in statuses.values()):
            return WARN
        return PASS

    @property
    def is_honeypot(self) -> bool:
        c = self.get("honeypot")
        return bool(c and c.status == FAIL)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["overall"] = self.overall
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SafetyReport":
        r = cls(d["chain"], d["address"], top10_pct=d.get("top10_pct"), market=d.get("market"),
                deployer=d.get("deployer"), holder_count=d.get("holder_count"))
        r.checks = [Check(**c) for c in d.get("checks") or []]
        return r


def _f(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _usd(v: float) -> str:
    if v >= 1e6:
        return f"${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:,.0f}"


# --- market -------------------------------------------------------------------

def check_market(report: SafetyReport, market: MarketInfo | None, th: dict) -> None:
    if market is None:
        report.add("liquidity", UNKNOWN, "no DEX pair data")
        report.add("pair_age", UNKNOWN, "no DEX pair data")
        return
    report.market = market.to_dict()
    liq = market.liquidity_usd
    if liq is None and (market.dex_id or "").lower() in BONDING_CURVE_DEXES:
        # Bonding curves often report no liquidity figure; the curve itself is the liquidity.
        mcap = market.market_cap or market.fdv
        report.add("liquidity", WARN, "bonding curve" + (f", mcap {_usd(mcap)}" if mcap else ""))
    elif liq is None:
        report.add("liquidity", UNKNOWN, "liquidity not reported")
    elif liq < th["min_liquidity_usd"]:
        report.add("liquidity", FAIL, f"{_usd(liq)} < min {_usd(th['min_liquidity_usd'])}")
    else:
        report.add("liquidity", PASS, _usd(liq))

    age = market.age_minutes
    if age is None:
        report.add("pair_age", UNKNOWN, "creation time not reported")
    elif age < th["warn_pair_age_minutes"]:
        report.add("pair_age", WARN, f"{age:.0f} min old - very new")
    else:
        report.add("pair_age", PASS, _fmt_age(age))


def _fmt_age(minutes: float) -> str:
    if minutes < 90:
        return f"{minutes:.0f} min"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f} h"
    return f"{minutes / 1440:.1f} d"


# --- solana / rugcheck ------------------------------------------------------

def apply_rugcheck(report: SafetyReport, data: dict | None, th: dict, dex_id: str | None) -> None:
    names = ("mint_authority", "freeze_authority", "lp_locked", "top10_holders", "rugcheck_risks")
    if not isinstance(data, dict) or not data:
        for n in names:
            report.add(n, UNKNOWN, "RugCheck unavailable")
        return

    token = data.get("token") or {}
    report.deployer = data.get("creator") or report.deployer
    hc = data.get("totalHolders") or data.get("holderCount")
    report.holder_count = int(hc) if isinstance(hc, (int, float)) else report.holder_count
    for key, name in (("mintAuthority", "mint_authority"), ("freezeAuthority", "freeze_authority")):
        if key in data or key in token:
            val = data.get(key, token.get(key))
            label = "mint" if name == "mint_authority" else "freeze"
            report.add(name, PASS if not val else FAIL, "revoked" if not val else f"{label} authority still active")
        else:
            report.add(name, UNKNOWN, "not reported")

    # LP lock: bonding curves have no LP that the dev can pull.
    if (dex_id or "").lower() in BONDING_CURVE_DEXES:
        report.add("lp_locked", PASS, "bonding curve (no LP yet)")
    else:
        pcts = [_f((m.get("lp") or {}).get("lpLockedPct")) for m in data.get("markets") or [] if isinstance(m, dict)]
        pcts = [p for p in pcts if p is not None]
        if not pcts:
            report.add("lp_locked", UNKNOWN, "no LP data")
        else:
            best = max(pcts)
            ok = best >= th["min_lp_locked_pct"]
            report.add("lp_locked", PASS if ok else WARN, f"{best:.0f}% locked/burned")

    known = data.get("knownAccounts") or {}
    excluded = {a for a, info in known.items() if isinstance(info, dict) and (info.get("type") or "").upper() != "CREATOR"}
    for m in data.get("markets") or []:
        if isinstance(m, dict):
            excluded.update(filter(None, (m.get("pubkey"), m.get("liquidityA"), m.get("liquidityB"))))
    holders = [h for h in data.get("topHolders") or [] if isinstance(h, dict)]
    if not holders:
        report.add("top10_holders", UNKNOWN, "no holder data")
    else:
        real = [h for h in holders if h.get("address") not in excluded and h.get("owner") not in excluded]
        top10 = sum(_f(h.get("pct")) or 0 for h in real[:10])
        report.top10_pct = round(top10, 2)
        ok = top10 <= th["max_top10_holder_pct"]
        insiders = sum(1 for h in real[:10] if h.get("insider"))
        extra = f", {insiders} insiders" if insiders else ""
        report.add("top10_holders", PASS if ok else FAIL,
                   f"top 10 hold {top10:.1f}% (max {th['max_top10_holder_pct']}%){extra}")

    risks = [r for r in data.get("risks") or [] if isinstance(r, dict)]
    danger = [r.get("name", "?") for r in risks if (r.get("level") or "").lower() == "danger"]
    warns = [r.get("name", "?") for r in risks if (r.get("level") or "").lower() == "warn"]
    if data.get("rugged"):
        report.add("rugcheck_risks", FAIL, "RugCheck marks token as RUGGED")
    elif danger:
        report.add("rugcheck_risks", FAIL, "; ".join(danger[:4]))
    elif warns:
        report.add("rugcheck_risks", WARN, "; ".join(warns[:4]))
    else:
        score = data.get("score_normalised", data.get("score"))
        report.add("rugcheck_risks", PASS, f"no risks flagged (score {score})" if score is not None else "no risks flagged")


# --- EVM / GoPlus -----------------------------------------------------------

def apply_goplus(report: SafetyReport, data: dict | None, th: dict, pair_address: str | None) -> None:
    names = ("honeypot", "taxes", "contract_controls", "top10_holders")
    if not isinstance(data, dict) or not data:
        for n in names:
            report.add(n, UNKNOWN, "GoPlus unavailable or token not indexed yet")
        return

    report.deployer = (data.get("creator_address") or "").lower() or report.deployer
    hc = _f(data.get("holder_count"))
    report.holder_count = int(hc) if hc is not None else report.holder_count

    def flag(key: str) -> bool | None:
        v = data.get(key)
        return None if v in (None, "") else str(v) == "1"

    hp, cant_sell = flag("is_honeypot"), flag("cannot_sell_all")
    if hp or cant_sell:
        report.add("honeypot", FAIL, "HONEYPOT - cannot sell" if hp else "cannot sell all tokens")
    elif hp is None:
        report.add("honeypot", UNKNOWN, "not reported")
    else:
        report.add("honeypot", PASS, "sellable")

    buy, sell = _f(data.get("buy_tax")), _f(data.get("sell_tax"))
    if buy is None and sell is None:
        report.add("taxes", UNKNOWN, "not reported")
    else:
        b, s = (buy or 0) * 100, (sell or 0) * 100
        bad = b > th["max_buy_tax_pct"] or s > th["max_sell_tax_pct"]
        report.add("taxes", FAIL if bad else PASS, f"buy {b:.1f}% / sell {s:.1f}%")

    fatal = [label for key, label in (
        ("owner_change_balance", "owner can change balances"),
        ("can_take_back_ownership", "ownership can be reclaimed"),
        ("hidden_owner", "hidden owner"),
        ("selfdestruct", "self-destruct"),
    ) if flag(key)]
    risky = [label for key, label in (
        ("is_mintable", "mintable"),
        ("is_proxy", "upgradeable proxy"),
        ("is_blacklisted", "blacklist function"),
        ("transfer_pausable", "transfers pausable"),
        ("slippage_modifiable", "tax modifiable"),
    ) if flag(key)]
    if flag("is_open_source") is False:
        risky.append("contract not verified")
    if fatal:
        report.add("contract_controls", FAIL, ", ".join(fatal + risky))
    elif risky:
        report.add("contract_controls", WARN, ", ".join(risky))
    else:
        report.add("contract_controls", PASS, "no dangerous owner controls")

    holders = [h for h in data.get("holders") or [] if isinstance(h, dict)]
    if not holders:
        report.add("top10_holders", UNKNOWN, "no holder data")
    else:
        skip = BURN_ADDRESSES | ({pair_address.lower()} if pair_address else set())
        real = [h for h in holders
                if (h.get("address") or "").lower() not in skip and str(h.get("is_locked")) != "1"]
        top10 = sum((_f(h.get("percent")) or 0) for h in real[:10]) * 100
        report.top10_pct = round(top10, 2)
        ok = top10 <= th["max_top10_holder_pct"]
        report.add("top10_holders", PASS if ok else FAIL,
                   f"top 10 hold {top10:.1f}% (max {th['max_top10_holder_pct']}%)")


# --- entry point ------------------------------------------------------------

class SafetyChecker:
    def __init__(self, http: Http, thresholds: dict):
        self.http = http
        self.th = thresholds

    async def rugcheck(self, mint: str) -> dict | None:
        r = await self.http.get_json(RUGCHECK_URL.format(mint=mint))
        return r.data if r.ok and isinstance(r.data, dict) else None

    async def goplus(self, chain: str, address: str) -> dict | None:
        chain_id = GOPLUS_CHAIN_IDS.get(chain)
        if not chain_id:
            return None
        r = await self.http.get_json(GOPLUS_URL.format(chain_id=chain_id),
                                     params={"contract_addresses": address})
        if not r.ok or not isinstance(r.data, dict) or str(r.data.get("code")) != "1":
            return None
        result = r.data.get("result") or {}
        return result.get(address.lower()) or next(iter(result.values()), None) if result else None

    async def check(self, chain: str, address: str, market: MarketInfo | None) -> SafetyReport:
        report = SafetyReport(chain, address)
        check_market(report, market, self.th)
        if chain == "solana":
            apply_rugcheck(report, await self.rugcheck(address), self.th, market.dex_id if market else None)
        elif chain in GOPLUS_CHAIN_IDS:
            apply_goplus(report, await self.goplus(chain, address), self.th,
                         market.pair_address if market else None)
        else:
            report.add("honeypot", UNKNOWN, f"no safety API for chain '{chain}'")
        log.info("safety %s:%s -> %s", chain, address, report.overall)
        return report
