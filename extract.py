"""Contract address (CA) extraction from free text and links.

- EVM: 0x + exactly 40 hex chars (not part of a longer hex string such as a tx hash).
- Solana: base58, 32-44 chars, must decode to exactly 32 bytes.
- Links: dexscreener / pump.fun / birdeye / etherscan-family / solscan.

Addresses inside a recognised link are classified by the link (a DexScreener
URL usually holds a *pair* address, not the token), so the plain-text scan
skips them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import base58

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_CLASS = f"[{B58}]"

EVM_RE = re.compile(r"(?<![0-9A-Za-z])0x[0-9a-fA-F]{40}(?![0-9A-Za-z])")
SOL_RE = re.compile(rf"(?<!{_B58_CLASS}){_B58_CLASS}{{32,44}}(?!{_B58_CLASS})")
URL_RE = re.compile(r"(?:https?://)?(?:www\.)?[a-z0-9.-]+\.[a-z]{2,}/[^\s<>\"')\]]+", re.I)

# Zero-width / invisible characters sometimes used to dodge scrapers or filters.
_INVISIBLE_RE = re.compile("[​‌‍⁠﻿­]")

# Addresses that are never meme-coin CAs (quote tokens, system programs, burn addresses).
IGNORED = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
    "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
    "0x4200000000000000000000000000000000000006",  # WETH (Base)
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
    "11111111111111111111111111111111",  # System program
    "So11111111111111111111111111111111111111112",  # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KLNBFCy3kR7gSMXomzpt",  # USDT
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token program
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022 program
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # Associated token program
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # pump.fun program
}

EXPLORER_CHAINS = {
    "etherscan.io": "ethereum",
    "basescan.org": "base",
    "bscscan.com": "bsc",
    "arbiscan.io": "arbitrum",
    "polygonscan.com": "polygon",
    "solscan.io": "solana",
}


@dataclass(frozen=True)
class Candidate:
    address: str
    chain_hint: str | None  # "solana", "evm", or a specific chain id
    kind: str = "token"  # token | dex (a DexScreener path: pair or token)
    url: str | None = None


def is_evm_address(s: str) -> bool:
    return bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", s))


def is_solana_address(s: str) -> bool:
    if not 32 <= len(s) <= 44 or any(c not in B58 for c in s):
        return False
    # Pure digit runs (e.g. concatenated IDs) are never real addresses in practice.
    if s.isdigit():
        return False
    try:
        return len(base58.b58decode(s)) == 32
    except ValueError:
        return False


def normalize(address: str) -> str | None:
    """Canonical form: EVM lowercased, Solana as-is. None if not a valid CA."""
    if is_evm_address(address):
        norm = address.lower()
    elif is_solana_address(address):
        norm = address
    else:
        return None
    return None if norm in IGNORED else norm


def chain_family(address: str) -> str | None:
    if is_evm_address(address):
        return "evm"
    if is_solana_address(address):
        return "solana"
    return None


def _from_url(raw_url: str) -> Candidate | None:
    url = raw_url if raw_url.lower().startswith("http") else "https://" + raw_url
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return None

    def cand(addr: str, chain: str | None, kind: str = "token") -> Candidate | None:
        norm = normalize(addr)
        if not norm:
            return None
        return Candidate(norm, chain or chain_family(norm), kind, url)

    if host == "dexscreener.com" and len(parts) >= 2:
        return cand(parts[1], parts[0].lower(), "dex")
    if host == "pump.fun":
        addr = parts[1] if parts[0] == "coin" and len(parts) > 1 else parts[0]
        return cand(addr, "solana")
    if host == "birdeye.so":
        # birdeye.so/token/<addr>?chain=solana  or  birdeye.so/<chain>/token/<addr>
        if "token" in parts:
            i = parts.index("token")
            if i + 1 < len(parts):
                chain = parse_qs(parsed.query).get("chain", [None])[0] or (parts[0] if i == 1 else None)
                return cand(parts[i + 1], chain.lower() if chain else None)
        return None
    if host in EXPLORER_CHAINS and len(parts) >= 2 and parts[0] in ("token", "address"):
        return cand(parts[1], EXPLORER_CHAINS[host])
    return None


def extract(text: str) -> list[Candidate]:
    """All distinct CAs in `text`, in order of first appearance.

    If the same address appears both bare and inside a link, the link version
    wins because it carries chain/kind info.
    """
    if not text:
        return []
    text = _INVISIBLE_RE.sub("", text)
    found: dict[str, Candidate] = {}
    first_pos: dict[str, int] = {}

    def add(pos: int, cand: Candidate, prefer: bool = False) -> None:
        first_pos[cand.address] = min(pos, first_pos.get(cand.address, pos))
        if prefer or cand.address not in found:
            found[cand.address] = cand

    def in_span(pos: int, spans: list[tuple[int, int]]) -> bool:
        return any(a <= pos < b for a, b in spans)

    link_spans: list[tuple[int, int]] = []
    for m in URL_RE.finditer(text):
        c = _from_url(m.group(0))
        if c:
            link_spans.append(m.span())
            add(m.start(), c, prefer=True)

    evm_spans: list[tuple[int, int]] = []
    for m in EVM_RE.finditer(text):
        evm_spans.append(m.span())
        norm = normalize(m.group(0))
        if norm and not in_span(m.start(), link_spans):
            add(m.start(), Candidate(norm, "evm"))

    for m in SOL_RE.finditer(text):
        if in_span(m.start(), link_spans) or in_span(m.start(), evm_spans):
            continue
        norm = normalize(m.group(0))
        if norm:
            add(m.start(), Candidate(norm, "solana"))

    return sorted(found.values(), key=lambda c: first_pos[c.address])
