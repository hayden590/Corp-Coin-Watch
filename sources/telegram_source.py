"""Telegram via Telethon, logged in as your own account (read-only use).

First run `python main.py telegram-login` once to create the local session
file (data/telegram.session - gitignored). After that the watcher reads new
messages from the channels in channels.yaml.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)


@dataclass
class TgMessage:
    chat_key: str       # channel username or numeric id as configured
    label: str
    weight: float
    msg_id: int
    text: str
    url: str | None
    date: float

    @property
    def source_ref(self) -> str:
        return f"{self.chat_key}/{self.msg_id}"


def channel_map(channels: list[dict]) -> dict[str, dict]:
    out = {}
    for c in channels or []:
        key = str(c.get("channel", "")).lstrip("@").lower()
        if key:
            out[key] = {"label": c.get("label") or key, "weight": float(c.get("weight", 1.0))}
    return out


def message_text(msg: Any) -> str:
    """Message text plus hidden hyperlink targets (CAs are often behind 'Chart' links)."""
    text = getattr(msg, "raw_text", None) or getattr(msg, "message", None) or ""
    urls = []
    for ent in getattr(msg, "entities", None) or []:
        url = getattr(ent, "url", None)
        if url:
            urls.append(url)
    markup = getattr(msg, "reply_markup", None)
    for row in getattr(markup, "rows", None) or []:
        for btn in getattr(row, "buttons", None) or []:
            if getattr(btn, "url", None):
                urls.append(btn.url)
    return "\n".join([text, *urls]).strip()


def to_message(msg: Any, chat: Any, channels: dict[str, dict]) -> TgMessage | None:
    username = (getattr(chat, "username", None) or "").lower()
    cid = str(getattr(chat, "id", ""))
    key = username if username in channels else cid if cid in channels else None
    if key is None:
        # Telethon marks channel ids as -100<id>
        alt = f"-100{cid}"
        key = alt if alt in channels else None
    if key is None:
        return None
    text = message_text(msg)
    if not text:
        return None
    date = getattr(msg, "date", None)
    return TgMessage(
        chat_key=key,
        label=channels[key]["label"],
        weight=channels[key]["weight"],
        msg_id=int(getattr(msg, "id", 0)),
        text=text,
        url=f"https://t.me/{username}/{msg.id}" if username else None,
        date=date.timestamp() if date else 0.0,
    )


class TelegramSource:
    def __init__(self, api_id: str, api_hash: str, session_path: Path, channels: list[dict]):
        self.api_id = int(api_id) if api_id else 0
        self.api_hash = api_hash
        self.session_path = session_path
        self.channels = channel_map(channels)
        self.client = None
        self.last_message_at: float | None = None

    def configured(self) -> bool:
        return bool(self.api_id and self.api_hash and self.channels)

    def _make_client(self):
        from telethon import TelegramClient  # optional dependency

        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        return TelegramClient(str(self.session_path), self.api_id, self.api_hash)

    async def login(self) -> None:
        """Interactive first login (asks for phone number + code in the terminal)."""
        client = self._make_client()
        await client.start()
        me = await client.get_me()
        print(f"Logged in to Telegram as {getattr(me, 'username', None) or me.id}. Session saved.")
        await client.disconnect()

    async def run(self, on_message: Callable[[TgMessage], Awaitable[None]]) -> None:
        from telethon import events

        self.client = self._make_client()
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Telegram session not logged in - run: python main.py telegram-login")
        chats = []
        for key in self.channels:
            try:
                chats.append(await self.client.get_entity(int(key) if key.lstrip("-").isdigit() else key))
            except Exception as exc:
                log.error("telegram: cannot open channel %s: %s", key, exc)

        async def handler(event):
            try:
                m = to_message(event.message, await event.get_chat(), self.channels)
                if m:
                    self.last_message_at = m.date
                    await on_message(m)
            except Exception:
                log.exception("telegram: failed handling a message")

        self.client.add_event_handler(handler, events.NewMessage(chats=chats))
        log.info("telegram: listening to %d channel(s)", len(chats))
        await self.client.run_until_disconnected()

    async def stop(self) -> None:
        if self.client:
            await self.client.disconnect()
