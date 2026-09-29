"""Desktop pop-up notifications you can click to open the coin.

  Windows 10/11: win11toast (pip)            -> click opens the link
  macOS:         terminal-notifier (brew)    -> click opens the link
                 (falls back to a plain notification without a click link)
  Linux:         notify-send                 -> link shown in the text

The OS decides where the pop-up appears (Windows: bottom-right, macOS: top-right).

Clicking a safe-looking alert opens `alerts.buy_link` (e.g. your Fomo token page)
so YOU can decide and buy by hand. DANGER / UNCHECKED alerts never link to a buy
page - they open DexScreener instead. This program still never trades.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys

log = logging.getLogger(__name__)

BUYABLE = {"VERIFIED", "UNCONFIRMED"}


def buy_link(template: str | None, chain: str, address: str) -> str | None:
    """Fill the configured buy-page template ({chain}, {address}); None if not set."""
    if not template:
        return None
    try:
        return template.format(chain=chain, address=address)
    except (KeyError, IndexError, ValueError):
        log.error("alerts.buy_link must only use {chain} and {address}")
        return None


def click_link(verdict: str | None, template: str | None, chain: str, address: str, dex_url: str | None) -> str | None:
    """Where a click should go. Unsafe coins never get a buy link."""
    if verdict in BUYABLE or verdict == "EXIT":
        return buy_link(template, chain, address) or dex_url
    return dex_url


def _applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class DesktopNotifier:
    def __init__(self, enabled: bool = True, platform: str | None = None):
        self.enabled = enabled
        self.platform = platform or sys.platform
        self._warned = False

    def backend(self) -> tuple[str | None, str]:
        """(backend name, human-readable status)"""
        if self.platform == "win32":
            try:
                import win11toast  # noqa: F401
                return "win11toast", "Windows notifications (click opens the coin)"
            except ImportError:
                return None, "run: pip install win11toast"
        if self.platform == "darwin":
            if shutil.which("terminal-notifier"):
                return "terminal-notifier", "macOS notifications (click opens the coin)"
            return "osascript", "macOS notifications WITHOUT click-to-open (run: brew install terminal-notifier)"
        if shutil.which("notify-send"):
            return "notify-send", "Linux notifications (link shown in the text; clicking can't open it)"
        return None, "no notification tool found (install libnotify / notify-send)"

    def command(self, backend: str, title: str, body: str, url: str | None) -> list[str] | None:
        """Command line for the subprocess-based backends (list form: no shell, no injection)."""
        if backend == "terminal-notifier":
            cmd = ["terminal-notifier", "-title", "corp-coin-watch", "-subtitle", title, "-message", body,
                   "-sound", "default"]
            return cmd + (["-open", url] if url else [])
        if backend == "osascript":
            return ["osascript", "-e",
                    f"display notification {_applescript_str(body)} with title {_applescript_str(title)} sound name \"default\""]
        if backend == "notify-send":
            return ["notify-send", "-a", "corp-coin-watch", title, body + (f"\n{url}" if url else "")]
        return None

    def _send(self, title: str, body: str, url: str | None) -> bool:
        backend, status = self.backend()
        if backend is None:
            if not self._warned:
                self._warned = True
                log.warning("desktop notifications unavailable: %s", status)
            return False
        if backend == "win11toast":
            from win11toast import notify

            notify(title, body, on_click=url or "", app_id="corp-coin-watch")
            return True
        cmd = self.command(backend, title, body, url)
        subprocess.run(cmd, check=False, timeout=10, capture_output=True)
        return True

    async def send(self, title: str, body: str, url: str | None) -> bool:
        if not self.enabled:
            return False
        try:
            return await asyncio.to_thread(self._send, title[:120], body[:240], url)
        except Exception as exc:  # a notification problem must never stop the bot
            log.warning("desktop notification failed: %s", exc)
            return False
