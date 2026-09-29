from safety import FAIL, PASS, UNKNOWN, WARN, SafetyReport
from scoring import DANGER, UNCHECKED, UNCONFIRMED, verdict


def report(**checks):
    r = SafetyReport("solana", "A")
    for name, st in checks.items():
        r.add(name, st, "d")
    return r


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
