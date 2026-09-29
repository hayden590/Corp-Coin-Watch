"""Shared async HTTP client: per-host rate limiting, retries with exponential
backoff on 429/5xx/network errors, and no exceptions for callers - a failed
request returns a FetchResult with ok=False so the pipeline marks the check
"unknown" instead of crashing.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "corp-coin-watch/0.1 (personal research bot; alerts only)"


@dataclass
class FetchResult:
    ok: bool
    status: int | None = None
    data: Any = None
    error: str | None = None


class HostLimiter:
    """Spaces requests to one host so we never exceed N per minute."""

    def __init__(self, per_minute: float):
        self.interval = 60.0 / max(per_minute, 0.1)
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next = max(now, self._next) + self.interval


class Http:
    def __init__(
        self,
        rate_limits: dict[str, float] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 3,
        base_backoff: float = 2.0,
        timeout: float = 20.0,
        on_request: Callable[[str], None] | None = None,
        default_per_minute: float = 30,
    ):
        self._limits = rate_limits or {}
        self.default_per_minute = default_per_minute
        self._limiters: dict[str, HostLimiter] = {}
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.on_request = on_request
        self.client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    def _limiter(self, host: str) -> HostLimiter:
        if host not in self._limiters:
            self._limiters[host] = HostLimiter(self._limits.get(host, self.default_per_minute))
        return self._limiters[host]

    async def request(self, method: str, url: str, *, expect_json: bool = True, **kwargs: Any) -> FetchResult:
        host = urlparse(url).netloc
        # Only log scheme+host+path: query strings / paths of webhooks can hold secrets,
        # so callers pass a `log_name` for anything sensitive.
        name = kwargs.pop("log_name", None) or f"{host}{urlparse(url).path}"
        max_retries = kwargs.pop("retries", self.max_retries)
        last_err = "unknown error"
        status = None
        for attempt in range(max_retries + 1):
            await self._limiter(host).wait()
            if self.on_request:
                self.on_request(host)
            try:
                resp = await self.client.request(method, url, **kwargs)
                status = resp.status_code
                if status == 429 or status >= 500:
                    last_err = f"HTTP {status}"
                    retry_after = _retry_after(resp)
                    if attempt < max_retries:
                        delay = retry_after if retry_after is not None else self._backoff(attempt)
                        log.warning("%s -> %s, retrying in %.1fs", name, last_err, delay)
                        await asyncio.sleep(delay)
                        continue
                    break
                if status >= 400:
                    return FetchResult(False, status, None, f"HTTP {status}")
                if not expect_json:
                    return FetchResult(True, status, resp.text)
                try:
                    return FetchResult(True, status, resp.json())
                except ValueError:
                    return FetchResult(False, status, None, "invalid JSON")
            except httpx.HTTPError as exc:
                last_err = f"{type(exc).__name__}"
                if attempt < max_retries:
                    delay = self._backoff(attempt)
                    log.warning("%s -> %s, retrying in %.1fs", name, last_err, delay)
                    await asyncio.sleep(delay)
                    continue
        log.log(logging.ERROR if max_retries else logging.INFO, "%s failed after %d attempt(s): %s",
                name, max_retries + 1, last_err)
        return FetchResult(False, status, None, last_err)

    def _backoff(self, attempt: int) -> float:
        return self.base_backoff * (2 ** attempt) * random.uniform(0.8, 1.2)

    async def get_json(self, url: str, **kwargs: Any) -> FetchResult:
        return await self.request("GET", url, **kwargs)

    async def post_json(self, url: str, payload: Any, **kwargs: Any) -> FetchResult:
        return await self.request("POST", url, json=payload, **kwargs)


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        # Discord puts it in the JSON body as seconds.
        try:
            body = resp.json()
            raw = body.get("retry_after") if isinstance(body, dict) else None
        except ValueError:
            return None
    try:
        return min(float(raw), 120.0) if raw is not None else None
    except (TypeError, ValueError):
        return None
