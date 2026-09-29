import base58

from extract import extract, is_solana_address, normalize

SOL = "6ce9TvjRyG4XEwjEcm16AXyf2hxrXtCEsth429Uk7MwU"
EVM = "0x74fa5327cc0f4e947789dd5e989a61a8242986a5"


def addrs(text):
    return [c.address for c in extract(text)]


# --- EVM ---------------------------------------------------------------------

def test_evm_basic_and_lowercased():
    assert addrs(f"CA: {EVM.upper().replace('0X', '0x')}") == [EVM]


def test_evm_wrong_length_rejected():
    assert addrs("0x" + "a" * 39) == []
    assert addrs("0x" + "a" * 41) == []


def test_evm_tx_hash_not_matched():
    assert addrs("tx 0x" + "ab" * 32) == []


def test_evm_glued_to_word_rejected():
    assert addrs("abc0x74fa5327cc0f4e947789dd5e989a61a8242986a5") == []


def test_evm_zero_and_quote_tokens_ignored():
    assert addrs("0x0000000000000000000000000000000000000000") == []
    assert addrs("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2") == []  # WETH


# --- Solana ------------------------------------------------------------------

def test_solana_valid():
    assert addrs(f"ape {SOL} now") == [SOL]


def test_solana_case_preserved():
    assert extract(SOL)[0].address == SOL


def test_solana_invalid_chars_rejected():
    bad = "0" + SOL[1:]  # '0' is not base58
    assert not is_solana_address(bad)
    assert not is_solana_address(SOL[:-1] + "l")  # 'l' not base58


def test_solana_must_decode_to_32_bytes():
    short = base58.b58encode(b"\x01" * 24).decode()  # valid base58, 24 bytes
    long_ = base58.b58encode(b"\x01" * 33).decode()
    assert not is_solana_address(short)
    assert not is_solana_address(long_)
    assert addrs(f"{short} {long_}") == []


def test_solana_length_bounds():
    assert addrs(SOL + "abc") == []  # 47 chars, too long
    assert addrs("A" * 31) == []


def test_solana_signature_not_matched():
    sig = base58.b58encode(b"\x07" * 64).decode()  # 87-88 chars
    assert addrs(sig) == []


def test_pure_digits_rejected():
    assert addrs("1" * 20 + "2" * 20) == []


def test_system_and_wsol_ignored():
    assert addrs("So11111111111111111111111111111111111111112 11111111111111111111111111111111") == []


# --- links -------------------------------------------------------------------

def test_dexscreener_link_is_dex_kind_with_chain():
    c = extract(f"https://dexscreener.com/ethereum/{EVM}")[0]
    assert (c.address, c.chain_hint, c.kind) == (EVM, "ethereum", "dex")


def test_pump_fun_links():
    for url in (f"https://pump.fun/coin/{SOL}", f"pump.fun/{SOL}", f"https://www.pump.fun/coin/{SOL}?ref=x"):
        c = extract(url)[0]
        assert (c.address, c.chain_hint, c.kind) == (SOL, "solana", "token"), url


def test_birdeye_links():
    assert extract(f"https://birdeye.so/token/{EVM}?chain=base")[0].chain_hint == "base"
    assert extract(f"https://birdeye.so/solana/token/{SOL}")[0].chain_hint == "solana"


def test_explorer_links():
    assert extract(f"https://basescan.org/token/{EVM}")[0].chain_hint == "base"
    assert extract(f"https://etherscan.io/address/{EVM}")[0].chain_hint == "ethereum"
    assert extract(f"https://solscan.io/token/{SOL}")[0].chain_hint == "solana"


def test_link_address_not_double_counted_and_dedup():
    text = f"https://dexscreener.com/solana/{SOL} again {SOL} and {SOL}"
    found = extract(text)
    assert len(found) == 1 and found[0].kind == "dex"


def test_multiple_distinct_in_order():
    assert addrs(f"{SOL} then {EVM}") == [SOL, EVM]


def test_zero_width_obfuscation_removed():
    obf = SOL[:10] + "​" + SOL[10:]
    assert addrs(obf) == [SOL]


def test_unrelated_links_ignored():
    assert addrs("https://x.com/someone/status/1234567890123456789") == []


def test_normalize():
    assert normalize(EVM.upper().replace("0X", "0x")) == EVM
    assert normalize("hello") is None


def test_bare_then_link_keeps_link_info_and_position():
    found = extract(f"{EVM} ... https://dexscreener.com/base/{EVM} and {SOL}")
    assert [c.address for c in found] == [EVM, SOL]
    assert found[0].kind == "dex" and found[0].chain_hint == "base"


def test_evm_like_run_not_matched_as_solana():
    # 0x + 40 hex without zeros is also a 41-char base58 run after the '0'
    addr = "0x" + "a1b2c3d4e5" * 4
    assert [c.chain_hint for c in extract(addr)] == ["evm"]
