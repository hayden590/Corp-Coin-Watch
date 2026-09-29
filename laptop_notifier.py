"""Laptop listener: shows the server's alerts as native pop-ups you can click.

The bot runs on your server and publishes each alert to your private ntfy topic.
This script (on your laptop) subscribes to that topic and turns every alert into a
Windows / macOS notification - clicking it opens the coin (your buy page or the
chart). It only needs to run while you want pop-ups on the laptop; your phone gets
alerts from the ntfy app whether the laptop is on or not.

    python laptop_notifier.py <your-topic>        (or put NTFY_TOPIC in .env)

Needs only: httpx, python-dotenv, and win11toast (Windows) or terminal-notifier (macOS).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import httpx

from desktop_notify import DesktopNotifier

log = logging.getLogger("laptop-notifier")


def to_popup(event: dict) -> tuple[str, str, str | None] | None:
    """ntfy stream line -> (title, body, click url). Ignores keepalives etc."""
    if event.get("event") != "message":
        return None
    click = event.get("click")
    if not click:
        views = [a.get("url") for a in event.get("actions") or [] if a.get("action") == "view"]
        click = views[0] if views else None
    if click and not str(click).startswith(("https://", "http://")):
        click = None  # only ever open web links
    return (str(event.get("title") or "corp-coin-watch")[:120], str(event.get("message") or "")[:240], click)


async def listen(server: str, topic: str, token: str = "", notifier: DesktopNotifier | None = None,
                 once: bool = False) -> None:
    notifier = notifier or DesktopNotifier(True)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    since = "30s"  # on (re)connect, catch anything sent while we were briefly offline
    backoff = 2.0
    while True:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=90)) as client:
                async with client.stream("GET", f"{server.rstrip('/')}/{topic}/json",
                                         params={"since": since}, headers=headers) as resp:
                    resp.raise_for_status()
                    log.info("connected - waiting for alerts on %s", topic)
                    backoff = 2.0
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if event.get("id"):
                            since = event["id"]
                        popup = to_popup(event)
                        if popup:
                            log.info("alert: %s", popup[0])
                            await notifier.send(*popup)
                            if once:
                                return
        except (httpx.HTTPError, OSError) as exc:
            log.warning("connection lost (%s); retrying in %.0fs", type(exc).__name__, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    topic = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("NTFY_TOPIC", "")).strip()
    if not topic:
        print("Usage: python laptop_notifier.py <your-ntfy-topic>   (make one with: python main.py new-ntfy-topic)")
        return 1
    backend, status = DesktopNotifier(True).backend()
    print(f"Pop-ups: {status}")
    if not backend:
        return 1
    try:
        asyncio.run(listen(os.getenv("NTFY_SERVER", "https://ntfy.sh"), topic, os.getenv("NTFY_TOKEN", "")))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
