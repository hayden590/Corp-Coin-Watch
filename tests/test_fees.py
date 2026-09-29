"""Global fees paid: normal, low-activity and wash-traded samples."""
import time

from fees import DEFAULT_JITO_TIP_ACCOUNTS, FeeResult, evaluate, sum_fees
from safety import PASS, UNKNOWN, WARN
from sources.dex_source import MarketInfo

CFG = {"min_global_fees_sol": 1.5, "min_fees_to_volume": 0.001, "wash_min_volume_usd": 20000}
JITO = set(DEFAULT_JITO_TIP_ACCOUNTS)
TIP = DEFAULT_JITO_TIP_ACCOUNTS[0]


def txs(n, fee, tip=0, every=1, span_min=60):
    now = time.time()
    out = []
    for i in range(n):
        nt = [{"toUserAccount": TIP, "amount": tip}] if tip and i % every == 0 else []
        out.append({"timestamp": now - span_min * 60 * i / n, "fee": fee, "nativeTransfers": nt})
    return out


def market(vol, buys=100, sells=100):
    return MarketInfo("solana", "A", volume_h24=vol, buys_h24=buys, sells_h24=sells, price_usd=0.001,
                      price_native=0.001 / 150, quote_symbol="SOL", pair_address="P")


def test_sum_fees_counts_base_priority_and_jito_only():
    t = [{"fee": 1_000_000, "nativeTransfers": [{"toUserAccount": TIP, "amount": 2_000_000},
                                                {"toUserAccount": "someone", "amount": 9_000_000_000}]}]
    assert sum_fees(t, JITO) == 0.003


def test_normal_token_passes():
    r = evaluate(market(200_000, 200, 200), txs(400, 100_000, 20_000_000, 2), True, 150, CFG, JITO)
    assert not r.low_activity and not r.wash_volume
    assert r.global_fees_sol > 1.5
    assert [c[1] for c in r.checks()] == [PASS, PASS]


def test_low_activity_token_filtered():
    r = evaluate(market(3_000, 20, 20), txs(40, 5_000), True, 150, CFG, JITO)
    assert r.low_activity
    assert r.checks()[0][1] == WARN and "LOW ACTIVITY" in r.checks()[0][2]


def test_wash_traded_token_warns():
    r = evaluate(market(2_000_000, 250, 250), txs(500, 5_000, 8_000_000, 2), True, 150, CFG, JITO)
    assert not r.low_activity and r.wash_volume
    assert "wash" in r.checks()[1][2]


def test_small_volume_is_never_called_wash():
    r = evaluate(market(5_000, 250, 250), txs(500, 5_000, 8_000_000, 2), True, 150, CFG, JITO)
    assert not r.wash_volume


def test_incomplete_sample_is_scaled_and_marked_estimate():
    r = evaluate(market(500_000, 2000, 2000), txs(500, 5_000, 10_000_000, 2), False, 150, CFG, JITO)
    assert r.estimated and r.global_fees_sol > sum_fees(txs(500, 5_000, 10_000_000, 2), JITO)
    assert r.checks()[0][2].startswith("~")


def test_unknown_never_filters():
    r = FeeResult("unknown", detail="needs HELIUS_API_KEY")
    assert not r.low_activity and r.checks()[0][1] == UNKNOWN


def test_no_indexed_swaps_is_unknown_not_low_activity():
    r = evaluate(market(50_000, 300, 200), [], True, 150, CFG, JITO)
    assert r.status == "unknown" and not r.low_activity
