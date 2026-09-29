"""Discord webhook / Telegram bot alerts.

Alerts are informational only - nothing here (or anywhere) can place a trade.
A CA is re-alerted only when its verdict changes.
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from typing import Any

from config import Secrets
from db import DB
from net import Http
from safety import FAIL, PASS, UNKNOWN, WARN, SafetyReport, _fmt_age, _usd
from scoring import DANGER, UNCHECKED, UNCONFIRMED, VERIFIED, Verdict

log = logging.getLogger(__name__)

STATUS_EMOJI = {PASS: "✅", WARN: "⚠️", FAIL: "❌", UNKNOWN: "❔"}
VERDICT_HEADLINE = {
    DANGER: "🚨 DANGER — DO NOT BUY",
    UNCHECKED: "❔ UNCHECKED — safety could not be verified, do not buy",
    UNCONFIRMED: "🟡 UNCONFIRMED — safety passed, no official confirmation",
    VERIFIED: "🟢 VERIFIED — official source confirms this CA",
}
DISCORD_COLORS = {DANGER: 0xE53935, UNCHECKED: 0x9E9E9E, UNCONFIRMED: 0xFDD835, VERIFIED: 0x43A047}
CHECK_LABELS = {
    "liquidity": "Liquidity",
    "pair_age": "Pair age",
    "mint_authority": "Mint authority",
    "freeze_authority": "Freeze authority",
    "lp_locked": "LP locked",
    "top10_holders": "Holder spread",
    "rugcheck_risks": "RugCheck",
    "honeypot": "Honeypot",
    "taxes": "Taxes",
    "contract_controls": "Contract",
}
FOOTER = "Not financial advice. Alerts only — this bot never trades."


@dataclass
class AlertContent:
    verdict: Verdict
    chain: str
    address: str
    name: str | None
    symbol: str | None
    safety: SafetyReport
    source: str
    source_url: str | None
    dex_url: str | None
    links: dict[str, list[str]] = field(default_factory=dict)

    @property
    def title(self) -> str:
        label = f"{self.name or 'Unknown token'}"
        return f"{label} (${self.symbol})" if self.symbol else label

    def market_lines(self) -> list[str]:
        m = self.safety.market or {}
        out = []
        if m.get("liquidity_usd") is not None:
            out.append(f"Liquidity: {_usd(m['liquidity_usd'])}")
        if m.get("age_minutes") is not None:
            out.append(f"Pair age: {_fmt_age(m['age_minutes'])}")
        if self.safety.top10_pct is not None:
            out.append(f"Top 10 holders: {self.safety.top10_pct:.1f}%")
        if m.get("fdv") is not None:
            out.append(f"FDV: {_usd(m['fdv'])}")
        if m.get("volume_h24") is not None:
            out.append(f"Vol 24h: {_usd(m['volume_h24'])}")
        if m.get("price_change_h1") is not None:
            out.append(f"1h: {m['price_change_h1']:+.1f}%")
        return out

    def check_lines(self) -> list[str]:
        return [f"{STATUS_EMOJI.get(c.status, '❔')} {CHECK_LABELS.get(c.name, c.name)}: {c.detail}"
                for c in self.safety.checks]

    def link_lines(self) -> list[str]:
        out = []
        for key, label in (("x", "X"), ("telegram", "Telegram"), ("website", "Website")):
            for url in (self.links.get(key) or [])[:2]:
                out.append(f"{label}: {url}")
        return out


def format_text(a: AlertContent) -> str:
    """Plain text (console / dry-run)."""
    lines = [
        "=" * 60,
        VERDICT_HEADLINE.get(a.verdict.label, a.verdict.label),
        f"{a.title} — {a.chain}",
        f"CA: {a.address}",
        f"Source: {a.source}" + (f" — {a.source_url}" if a.source_url else ""),
    ]
    if a.verdict.is_danger:
        lines.append("Why: " + "; ".join(a.verdict.reasons))
    lines.append("Checks:")
    lines += [f"  {line}" for line in a.check_lines()]
    if ml := a.market_lines():
        lines.append("Market: " + " | ".join(ml))
    lines += a.link_lines()
    if a.dex_url:
        lines.append(f"DexScreener: {a.dex_url}")
    lines.append(FOOTER)
    return "\n".join(lines)


def format_discord(a: AlertContent) -> dict[str, Any]:
    fields = [{"name": "CA", "value": f"`{a.address}`", "inline": False}]
    if a.verdict.is_danger:
        fields.append({"name": "Why", "value": _clip("\n".join(a.verdict.reasons), 1000), "inline": False})
    fields.append({"name": "Checks", "value": _clip("\n".join(a.check_lines()) or "—", 1000), "inline": False})
    if ml := a.market_lines():
        fields.append({"name": "Market", "value": _clip(" | ".join(ml), 1000), "inline": False})
    if ll := a.link_lines():
        fields.append({"name": "Links", "value": _clip("\n".join(ll), 1000), "inline": False})
    src = a.source + (f" — {a.source_url}" if a.source_url else "")
    fields.append({"name": "Source", "value": _clip(src, 1000), "inline": False})
    embed = {
        "title": _clip(f"{VERDICT_HEADLINE.get(a.verdict.label, a.verdict.label)}", 256),
        "description": _clip(f"**{a.title}** on **{a.chain}**", 2000),
        "color": DISCORD_COLORS.get(a.verdict.label, 0x9E9E9E),
        "fields": fields,
        "footer": {"text": FOOTER},
    }
    if a.dex_url:
        embed["url"] = a.dex_url
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


def format_telegram(a: AlertContent) -> str:
    e = html.escape
    parts = [
        f"<b>{e(VERDICT_HEADLINE.get(a.verdict.label, a.verdict.label))}</b>",
        f"<b>{e(a.title)}</b> — {e(a.chain)}",
        f"CA: <code>{e(a.address)}</code>",
    ]
    if a.verdict.is_danger:
        parts.append("Why: " + e("; ".join(a.verdict.reasons)))
    parts.append("\n".join(e(line) for line in a.check_lines()))
    if ml := a.market_lines():
        parts.append(e(" | ".join(ml)))
    if ll := a.link_lines():
        parts.append("\n".join(e(line) for line in ll))
    parts.append(f"Source: {e(a.source)}" + (f" — {e(a.source_url)}" if a.source_url else ""))
    if a.dex_url:
        parts.append(f'<a href="{e(a.dex_url, quote=True)}">DexScreener</a>')
    parts.append(f"<i>{e(FOOTER)}</i>")
    return _clip("\n".join(parts), 4000)


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


class Alerter:
    def __init__(self, http: Http, secrets: Secrets, cfg: dict, db: DB, console_only: bool = False):
        self.http = http
        self.secrets = secrets
        self.db = db
        acfg = cfg.get("alerts") or {}
        self.console_only = console_only
        self.use_discord = bool(acfg.get("discord", True) and secrets.discord_webhook_url) and not console_only
        self.use_telegram = bool(
            acfg.get("telegram", True) and secrets.telegram_bot_token and secrets.telegram_chat_id
        ) and not console_only
        if not console_only and not (self.use_discord or self.use_telegram):
            log.warning("No alert channel configured (set DISCORD_WEBHOOK_URL and/or TELEGRAM_BOT_TOKEN+CHAT_ID); "
                        "alerts will be printed to the console only")

    def should_alert(self, chain: str, address: str, verdict_label: str) -> bool:
        return self.db.last_alert_verdict(chain, address) != verdict_label

    async def send_verdict(self, a: AlertContent) -> bool:
        if not self.should_alert(a.chain, a.address, a.verdict.label):
            log.info("skip alert %s:%s - verdict %s unchanged", a.chain, a.address, a.verdict.label)
            return False
        sent = await self._deliver(format_text(a), format_discord(a), format_telegram(a))
        if not sent:
            return False  # not recorded, so the next pass retries
        self.db.record_alert(a.chain, a.address, "verdict", a.verdict.label, sent,
                             {"title": a.title, "reasons": a.verdict.reasons})
        return True

    async def send_system(self, message: str) -> None:
        text = f"⚙️ corp-coin-watch: {message}"
        await self._deliver(text, {"content": _clip(text, 1900), "allowed_mentions": {"parse": []}}, html.escape(text))
        self.db.record_alert("-", "-", "system", None, [], {"message": message})

    async def _deliver(self, text: str, discord_payload: dict, telegram_html: str) -> list[str]:
        sent: list[str] = []
        if not (self.use_discord or self.use_telegram):
            print(text, flush=True)
            return ["console"]
        if self.use_discord:
            r = await self.http.post_json(self.secrets.discord_webhook_url, discord_payload,
                                          expect_json=False, log_name="discord-webhook")
            if r.ok:
                sent.append("discord")
            else:
                log.error("Discord alert failed: %s", r.error)
        if self.use_telegram:
            url = f"https://api.telegram.org/bot{self.secrets.telegram_bot_token}/sendMessage"
            r = await self.http.post_json(url, {
                "chat_id": self.secrets.telegram_chat_id,
                "text": telegram_html,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, log_name="telegram-bot")
            if r.ok:
                sent.append("telegram")
            else:
                log.error("Telegram alert failed: %s", r.error)
        return sent
