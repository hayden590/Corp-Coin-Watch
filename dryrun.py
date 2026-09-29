"""--dry-run support: an httpx transport that answers every API call from the
JSON files in sample_data/, so the full pipeline runs with no keys or network.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx

SAMPLE_DIR = Path(__file__).resolve().parent / "sample_data"


def _load(name: str, sample_dir: Path) -> dict | list:
    data = json.loads((sample_dir / name).read_text())
    if isinstance(data, dict):
        data.pop("_comment", None)
    return data


def _materialise_pair(p: dict) -> dict:
    p = dict(p)
    ago = p.pop("pairCreatedMinutesAgo", None)
    if ago is not None:
        p["pairCreatedAt"] = int((time.time() - ago * 60) * 1000)
    return p


class SampleTransport(httpx.AsyncBaseTransport):
    def __init__(self, sample_dir: Path = SAMPLE_DIR):
        self.profiles = _load("dex_profiles.json", sample_dir)
        self.pairs = {k.lower(): [_materialise_pair(p) for p in v]
                      for k, v in _load("dex_pairs.json", sample_dir).items()}
        self.rugcheck = _load("rugcheck.json", sample_dir)
        self.goplus = _load("goplus.json", sample_dir)
        self.calls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        host, path = url.host, url.path
        self.calls.append(f"{request.method} {host}{path}")

        if host == "api.dexscreener.com":
            if path == "/token-profiles/latest/v1":
                return httpx.Response(200, json=self.profiles)
            if m := re.fullmatch(r"/latest/dex/tokens/([^/]+)", path):
                return httpx.Response(200, json={"schemaVersion": "1.0.0",
                                                 "pairs": self.pairs.get(m.group(1).lower()) or None})
            if m := re.fullmatch(r"/latest/dex/pairs/([^/]+)/([^/]+)", path):
                pair_addr = m.group(2).lower()
                hits = [p for ps in self.pairs.values() for p in ps
                        if (p.get("pairAddress") or "").lower() == pair_addr]
                return httpx.Response(200, json={"pairs": hits or None})
        if host == "api.rugcheck.xyz":
            if m := re.fullmatch(r"/v1/tokens/([^/]+)/report", path):
                data = self.rugcheck.get(m.group(1))
                return httpx.Response(200, json=data) if data else httpx.Response(404, json={"error": "not found"})
        if host == "api.gopluslabs.io":
            addr = (url.params.get("contract_addresses") or "").lower()
            data = self.goplus.get(addr)
            return httpx.Response(200, json={"code": 1, "message": "OK", "result": {addr: data} if data else {}})
        if host in ("discord.com", "api.telegram.org"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"error": f"no sample for {host}{path}"})


def sample_messages(sample_dir: Path = SAMPLE_DIR) -> list[dict]:
    return _load("messages.json", sample_dir)
