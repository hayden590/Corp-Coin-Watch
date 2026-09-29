import time

from safety import FAIL, PASS, UNKNOWN, WARN, SafetyReport, apply_goplus, apply_rugcheck, check_market
from sources.dex_source import MarketInfo

TH = {"min_liquidity_usd": 10000, "max_top10_holder_pct": 30, "max_buy_tax_pct": 10,
      "max_sell_tax_pct": 10, "min_lp_locked_pct": 80, "warn_pair_age_minutes": 10}


def status(report, name):
    return report.get(name).status


def market(**kw):
    base = dict(chain="solana", address="A", liquidity_usd=50000, pair_created_at=time.time() - 3600, dex_id="raydium")
    base.update(kw)
    return MarketInfo(**base)


def test_market_liquidity_and_age():
    r = SafetyReport("solana", "A")
    check_market(r, market(liquidity_usd=5000, pair_created_at=time.time() - 60), TH)
    assert status(r, "liquidity") == FAIL
    assert status(r, "pair_age") == WARN


def test_market_missing_is_unknown():
    r = SafetyReport("solana", "A")
    check_market(r, None, TH)
    assert status(r, "liquidity") == UNKNOWN and r.overall == UNKNOWN


def test_bonding_curve_without_liquidity_warns():
    r = SafetyReport("solana", "A")
    check_market(r, market(liquidity_usd=None, dex_id="pumpfun", market_cap=30000), TH)
    assert status(r, "liquidity") == WARN


def _rug(**kw):
    d = {"mintAuthority": None, "freezeAuthority": None, "risks": [],
         "markets": [{"pubkey": "POOL", "lp": {"lpLockedPct": 100}}],
         "knownAccounts": {"AMM1": {"type": "AMM"}, "DEV": {"type": "CREATOR"}},
         "topHolders": [{"address": "x", "owner": "AMM1", "pct": 40}] +
                       [{"address": f"h{i}", "owner": f"o{i}", "pct": 2} for i in range(12)]}
    d.update(kw)
    return d


def test_rugcheck_clean_passes_and_excludes_amm():
    r = SafetyReport("solana", "A")
    check_market(r, market(), TH)
    apply_rugcheck(r, _rug(), TH, "raydium")
    assert r.top10_pct == 20.0
    assert r.overall == PASS


def test_rugcheck_creator_holdings_count():
    r = SafetyReport("solana", "A")
    apply_rugcheck(r, _rug(topHolders=[{"address": "d", "owner": "DEV", "pct": 35}]), TH, "raydium")
    assert status(r, "top10_holders") == FAIL


def test_rugcheck_authorities_fail():
    r = SafetyReport("solana", "A")
    apply_rugcheck(r, _rug(token={"mintAuthority": "X", "freezeAuthority": "Y"}, mintAuthority=None), TH, "raydium")
    # top-level null wins when present
    assert status(r, "mint_authority") == PASS
    r2 = SafetyReport("solana", "A")
    d = _rug()
    del d["mintAuthority"], d["freezeAuthority"]
    d["token"] = {"mintAuthority": "X", "freezeAuthority": "Y"}
    apply_rugcheck(r2, d, TH, "raydium")
    assert status(r2, "mint_authority") == FAIL and status(r2, "freeze_authority") == FAIL


def test_rugcheck_danger_and_rugged():
    r = SafetyReport("solana", "A")
    apply_rugcheck(r, _rug(risks=[{"name": "Bad", "level": "danger"}]), TH, "raydium")
    assert status(r, "rugcheck_risks") == FAIL
    r2 = SafetyReport("solana", "A")
    apply_rugcheck(r2, _rug(rugged=True), TH, "raydium")
    assert "RUGGED" in r2.get("rugcheck_risks").detail


def test_rugcheck_lp_unlocked_warns_but_bonding_curve_passes():
    r = SafetyReport("solana", "A")
    apply_rugcheck(r, _rug(markets=[{"lp": {"lpLockedPct": 10}}]), TH, "raydium")
    assert status(r, "lp_locked") == WARN
    r2 = SafetyReport("solana", "A")
    apply_rugcheck(r2, _rug(markets=[]), TH, "pumpfun")
    assert status(r2, "lp_locked") == PASS


def test_rugcheck_unavailable_is_unknown_not_pass():
    r = SafetyReport("solana", "A")
    check_market(r, market(), TH)
    apply_rugcheck(r, None, TH, "raydium")
    assert r.overall == UNKNOWN


def _gp(**kw):
    d = {"is_honeypot": "0", "cannot_sell_all": "0", "buy_tax": "0", "sell_tax": "0.02", "is_open_source": "1",
         "holders": [{"address": "0xPAIR", "percent": "0.5"},
                     {"address": "0x000000000000000000000000000000000000dEaD", "percent": "0.2"},
                     {"address": "0xlocker", "percent": "0.1", "is_locked": 1},
                     {"address": "0xa", "percent": "0.05"}]}
    d.update(kw)
    return d


def test_goplus_clean_and_holder_exclusions():
    r = SafetyReport("base", "0x1")
    check_market(r, market(chain="base"), TH)
    apply_goplus(r, _gp(), TH, "0xpair")
    assert r.top10_pct == 5.0
    assert r.overall == PASS


def test_goplus_honeypot_and_tax():
    r = SafetyReport("base", "0x1")
    apply_goplus(r, _gp(is_honeypot="1", sell_tax="0.25"), TH, None)
    assert r.is_honeypot and status(r, "taxes") == FAIL and r.overall == FAIL


def test_goplus_owner_controls():
    r = SafetyReport("base", "0x1")
    apply_goplus(r, _gp(is_mintable="1", is_proxy="1"), TH, None)
    assert status(r, "contract_controls") == WARN
    r2 = SafetyReport("base", "0x1")
    apply_goplus(r2, _gp(owner_change_balance="1"), TH, None)
    assert status(r2, "contract_controls") == FAIL


def test_goplus_missing_fields_unknown():
    r = SafetyReport("base", "0x1")
    apply_goplus(r, {"holders": []}, TH, None)
    assert status(r, "honeypot") == UNKNOWN and status(r, "taxes") == UNKNOWN
    r2 = SafetyReport("base", "0x1")
    apply_goplus(r2, None, TH, None)
    assert r2.overall == UNKNOWN


def test_report_roundtrip():
    r = SafetyReport("base", "0x1", top10_pct=5.0)
    r.add("liquidity", PASS, "$50K")
    assert SafetyReport.from_dict(r.to_dict()).to_dict() == r.to_dict()
