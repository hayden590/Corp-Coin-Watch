import time

from graph import ConnectionReport
from safety import FAIL, PASS, UNKNOWN, WARN, SafetyReport
from scoring import (DANGER, UNCHECKED, UNCONFIRMED, VERIFIED, Assessment, backing_score, connection_score,
                     should_alert, text_points, verdict)
from sources.x_source import XUser
from text_analysis import TextResult
from verify import LegitReport
from wallets import WalletRecord

W = {}


def report(**checks):
    r = SafetyReport("solana", "A")
    for name, st in checks.items():
        r.add(name, st, "d")
    return r


def ok_safety():
    return report(liquidity=PASS, top10_holders=PASS, mint_authority=PASS)


# --- verdicts -----------------------------------------------------------------

def test_any_fail_is_danger():
    v = verdict(report(liquidity=PASS, top10_holders=PASS, mint_authority=FAIL))
    assert v.label == DANGER and v.is_danger and "mint_authority" in v.reasons[0]


def test_fail_beats_unknown():
    assert verdict(report(liquidity=UNKNOWN, honeypot=FAIL)).label == DANGER


def test_critical_unknown_is_unchecked():
    assert verdict(report(liquidity=PASS, top10_holders=UNKNOWN)).label == UNCHECKED


def test_noncritical_unknown_and_warn_is_unconfirmed():
    assert verdict(report(liquidity=PASS, top10_holders=PASS, lp_locked=UNKNOWN, pair_age=WARN)).label == UNCONFIRMED


def test_empty_report_is_unchecked():
    assert verdict(SafetyReport("solana", "A")).label == UNCHECKED


def legit(official=False, persisted=None, **checks):
    l = LegitReport(official_confirmed=official, persisted=persisted)
    for n, st in checks.items():
        l.add(n, st, "d")
    return l


def test_verified_needs_official_site_and_persisted_tweet_and_safety():
    a = Assessment("solana", "A", ok_safety(), legit=legit(True, True))
    assert verdict(a.safety, a).label == VERIFIED
    a = Assessment("solana", "A", ok_safety(), legit=legit(True, None))  # tweet not re-checked yet
    assert verdict(a.safety, a).label == UNCONFIRMED
    a = Assessment("solana", "A", ok_safety(), legit=legit(False, True))
    assert verdict(a.safety, a).label == UNCONFIRMED


def test_deleted_tweet_and_account_changes_are_danger_even_if_official():
    for check in ("tweet_persists", "account_integrity"):
        a = Assessment("solana", "A", ok_safety(), legit=legit(True, True, **{check: FAIL}))
        assert verdict(a.safety, a).label == DANGER


def test_insider_dump_and_hijack_are_danger():
    a = Assessment("solana", "A", ok_safety(), dump_flags=["@x wallet SOLD 5 min after posting it"])
    assert verdict(a.safety, a).label == DANGER
    c = ConnectionReport(hijack_flags=["@x was renamed"])
    a = Assessment("solana", "A", ok_safety(), connections=c)
    assert verdict(a.safety, a).label == DANGER
    assert verdict(a.safety, a, hijack_is_danger=False).label == UNCONFIRMED


def test_no_score_overrides_danger():
    a = Assessment("solana", "A", report(liquidity=PASS, honeypot=FAIL), legit=legit(True, True))
    a.backing, a.connection = 99, 99
    assert verdict(a.safety, a).label == DANGER


# --- backing ----------------------------------------------------------------------

def rec(mult, win=0.5):
    return WalletRecord("w", "solana", "whale", "manual", 20, 20, win, 0.0, 1.0, mult)


def test_backing_breakdown_and_weights():
    ends = [{"user_id": "1", "handle": "mega", "tier": 1, "weight": 1.0, "kind": "quote"},
            {"user_id": "2", "handle": "trader", "tier": 2, "weight": 2.0, "kind": "post"},
            {"user_id": "2", "handle": "trader", "tier": 2, "weight": 2.0, "kind": "reply"}]  # same user counted once
    total, parts = backing_score(ends, [{"label": "Alpha", "weight": 1.0}], 10, ["scammer"],
                                 [{"wallet": "w", "label": "whale", "sold": False, "record": rec(1.5)},
                                  {"wallet": "v", "label": "new", "sold": False, "record": rec(0.0)}], 0, W)
    labels = dict(parts)
    assert labels["tier-1 @mega (quote)"] == 3.0
    assert labels["tier-2 @trader (post)"] == 3.0
    assert labels["telegram Alpha"] == 1.0
    assert labels["10 other X poster(s)"] == 1.0  # capped
    assert labels["blacklisted poster @scammer"] == -1.0
    assert any("not enough history" in l and p == 0 for l, p in parts)  # untrusted wallet adds nothing
    assert total == 3.0 + 3.0 + 1.0 + 1.0 - 1.0 + 1.5


# --- connections ------------------------------------------------------------------

def creator():
    return XUser("5", "creator", created_at=time.time() - 400 * 86400, followers=5000)


def test_connection_recent_interaction_beats_old():
    new = ConnectionReport(creator=creator(), interactions=[{"handle": "m", "tier": 1, "kind": "reply", "days_ago": 1}])
    old = ConnectionReport(creator=creator(), interactions=[{"handle": "m", "tier": 1, "kind": "reply", "days_ago": 80}])
    assert connection_score(new, W)[0] > connection_score(old, W)[0] * 5


def test_connection_zero_without_confirmed_creator_or_with_hijack():
    assert connection_score(ConnectionReport(tier1_followers=["m"]), W)[0] == 0
    c = ConnectionReport(creator=creator(), tier1_followers=["m"], hijack_flags=["renamed"])
    assert connection_score(c, W)[0] == 0


# --- alert gating -------------------------------------------------------------------

def test_connection_and_text_alone_never_alert():
    a = Assessment("solana", "A", ok_safety())
    a.verdict = verdict(a.safety, a)
    a.connection, a.text_points, a.chart_points, a.backing = 50, 5, 5, 0
    assert should_alert(a, {"dexscreener"}, {"alerts": {"min_backing_to_alert": 1.0}})[0] is False
    a.backing = 1.0
    assert should_alert(a, {"dexscreener"}, {"alerts": {"min_backing_to_alert": 1.0}})[0] is True


def test_low_activity_blocks_alert_but_not_danger():
    from fees import FeeResult

    a = Assessment("solana", "A", ok_safety(), fees=FeeResult("ok", 0.2, 0.2, low_activity=True))
    a.verdict = verdict(a.safety, a)
    a.backing = 5
    assert should_alert(a, {"telegram"}, {"alerts": {}})[0] is False
    d = Assessment("solana", "A", report(liquidity=PASS, honeypot=FAIL), fees=FeeResult("ok", 0.2, low_activity=True))
    d.verdict = verdict(d.safety, d)
    assert should_alert(d, {"telegram"}, {"alerts": {"danger_alert_sources": ["telegram"]}})[0] is True


def test_text_is_minor_and_bounded():
    t = TextResult(n_texts=10, sentiment=1.0, hype_vs_substance=0.0)
    assert text_points(t, {"text_weight": 0.5}) == 0.5
    t = TextResult(n_texts=10, sentiment=-1.0, hype_vs_substance=0.0, bot_like=True, red_flags=["x"])
    assert text_points(t, {"text_weight": 0.5}) == -0.5
