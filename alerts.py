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
from desktop_notify import BUYABLE, DesktopNotifier, buy_link, click_link
from db import DB
from net import Http
from safety import FAIL, PASS, UNKNOWN, WARN, SafetyReport, _fmt_age, _usd
from scoring import DANGER, UNCHECKED, UNCONFIRMED, VERIFIED, Assessment, Verdict

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
    "tweet_persists": "Tweet persists",
    "account_integrity": "Account integrity",
    "official_website": "Official website",
    "first_time_poster": "First-time poster",
    "narrative": "Narrative",
    "global_fees": "Global fees",
    "wash_volume": "Volume vs fees",
}
FOOTER = "Not financial advice. Alerts only — this bot never trades."


@dataclass
class AlertContent:
    a: Assessment
    name: str | None
    symbol: str | None
    source: str
    source_url: str | None
    dex_url: str | None
    links: dict[str, list[str]] = field(default_factory=dict)
    buy_url: str | None = None  # only ever set for VERIFIED / UNCONFIRMED

    @property
    def verdict(self) -> Verdict:
        return self.a.verdict

    @property
    def chain(self) -> str:
        return self.a.chain

    @property
    def address(self) -> str:
        return self.a.address

    @property
    def safety(self) -> SafetyReport:
        return self.a.safety

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
        f = self.a.fees
        if f is not None and f.status == "ok":
            ratio = f" | fees/vol {f.fees_to_volume * 100:.3f}%" if f.fees_to_volume is not None else ""
            out.append(f"Global fees: {f.global_fees_sol:.2f} SOL{ratio}")
        return out

    def check_lines(self) -> list[str]:
        checks = list(self.safety.checks)
        if self.a.legit is not None:
            checks += self.a.legit.checks
        lines = [f"{STATUS_EMOJI.get(c.status, '❔')} {CHECK_LABELS.get(c.name, c.name)}: {c.detail}" for c in checks]
        if self.a.fees is not None:
            lines += [f"{STATUS_EMOJI.get(st, '❔')} {CHECK_LABELS.get(n, n)}: {d}" for n, st, d in self.a.fees.checks()]
        return lines

    def backing_lines(self) -> list[str]:
        if not self.a.backing_breakdown:
            return [f"Backing {self.a.backing:+.1f}: nobody we track is backing it yet"]
        return [f"Backing {self.a.backing:+.1f}:"] + [f"  {p:+.1f} {l}" for l, p in self.a.backing_breakdown[:8]]

    def connection_line(self) -> str | None:
        c = self.a.connections
        if c is None:
            return None
        parts = []
        if c.creator:
            how = next((x.how for x in c.linked if x.user and x.user.id == c.creator.id), "")
            parts.append(f"Creator: @{c.creator.handle} ({how})")
            parts.append("Creator followed by: " + (", ".join(f"@{h}" for h in c.tier1_followers[:4]) or "no tier-1")
                         + (f" + {c.tier2_followers} tier-2" if c.tier2_followers else ""))
            if c.interactions:
                i = c.interactions[0]
                parts.append(f"Recent interaction: @{i['handle']} {i['kind']} {i['days_ago']:.0f}d ago")
            else:
                parts.append("Recent interaction: none")
            if c.creator.age_days is not None:
                parts.append(f"Account age: {c.creator.age_days / 365:.1f}y, {c.creator.followers:,} followers")
            if c.follower_quality:
                fq = c.follower_quality
                parts.append(f"Follower sample: {fq['no_pfp_pct']:.0f}% no pfp, {fq['zero_tweets_pct']:.0f}% no tweets, "
                             f"{fq['new_pct']:.0f}% <30d old")
        elif c.linked:
            parts.append("No confirmed creator")
        if c.deployer_summary:
            parts.append(f"Deployer: {c.deployer_summary}")
        if c.warnings:
            parts.append("Warnings: " + "; ".join(c.warnings[:4]))
        if c.note:
            parts.append(c.note)
        return " | ".join(parts) if parts else None

    def insight_lines(self) -> list[str]:
        out = []
        if (cl := self.connection_line()):
            out.append(f"Connections: {cl} (connection score {self.a.connection:+.1f})")
        if self.a.chart is not None:
            out.append(f"Chart: {self.a.chart.summary}")
            out.append(f"Entry quality: {self.a.chart.entry}")
        if self.a.exit_flags:
            out.append("ACTIVE EXIT WARNINGS: " + "; ".join(d for _, d in self.a.exit_flags))
        if self.a.text is not None:
            t = self.a.text.summary
            if self.a.text.main_claims:
                t += " | claims: " + "; ".join(self.a.text.main_claims[:2])
            out.append(f"Chatter: {t}")
        if self.a.similar:
            out.append(f"Similar setups: {self.a.similar['text']}")
        return out

    def desktop(self) -> tuple[str, str]:
        """Short title + body for a desktop pop-up."""
        m = self.safety.market or {}
        bits = [self.chain]
        if m.get("liquidity_usd") is not None:
            bits.append(f"liq {_usd(m['liquidity_usd'])}")
        if m.get("age_minutes") is not None:
            bits.append(_fmt_age(m["age_minutes"]))
        bits.append(f"backing {self.a.backing:+.1f}")
        head = VERDICT_HEADLINE.get(self.verdict.label, self.verdict.label).split(" — ")[0]
        body = " | ".join(bits)
        if self.verdict.is_danger or self.verdict.label == UNCHECKED:
            body = "DO NOT BUY. " + body + ". Click for the chart."
        else:
            body += ". Click to open " + ("the buy page." if self.buy_url else "the chart.")
        return f"{head} {self.title}", body

    def link_lines(self) -> list[str]:
        out = [f"Open to buy yourself: {self.buy_url}"] if self.buy_url else []
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
    lines += a.backing_lines()
    lines += a.insight_lines()
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
    fields.append({"name": "Backing", "value": _clip("\n".join(a.backing_lines()), 1000), "inline": False})
    for line in a.insight_lines():
        name, _, value = line.partition(": ")
        fields.append({"name": _clip(name, 250), "value": _clip(value or "—", 1000), "inline": False})
    if ll := a.link_lines():
        fields.append({"name": "Links", "value": _clip("\n".join(ll), 1000), "inline": False})
    src = a.source + (f" — {a.source_url}" if a.source_url else "")
    fields.append({"name": "Source", "value": _clip(src, 1000), "inline": False})
    embed = {
        "title": _clip(f"{VERDICT_HEADLINE.get(a.verdict.label, a.verdict.label)}", 256),
        "description": _clip(f"**{a.title}** on **{a.chain}**", 2000),
        "color": DISCORD_COLORS.get(a.verdict.label, 0x9E9E9E),
        "fields": fields[:25],
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
    parts.append("\n".join(e(line) for line in a.backing_lines()))
    if il := a.insight_lines():
        parts.append("\n".join(e(line) for line in il))
    if ll := a.link_lines():
        parts.append("\n".join(e(line) for line in ll))
    parts.append(f"Source: {e(a.source)}" + (f" — {e(a.source_url)}" if a.source_url else ""))
    if a.dex_url:
        parts.append(f'<a href="{e(a.dex_url, quote=True)}">DexScreener</a>')
    parts.append(f"<i>{e(FOOTER)}</i>")
    return _clip("\n".join(parts), 4000)


def format_exit_warning(chain: str, address: str, title: str, flags: list[tuple[str, str]], dex_url: str | None) -> tuple[str, dict, str]:
    head = f"⚠️ EXIT WARNING — {title} ({chain})"
    body = [f"CA: {address}"] + [f"• {d}" for _, d in flags] + ([f"DexScreener: {dex_url}"] if dex_url else []) + [FOOTER]
    text = "\n".join(["=" * 60, head, *body])
    discord = {"embeds": [{"title": _clip(head, 256), "description": _clip("\n".join(body), 3900), "color": 0xFB8C00}],
               "allowed_mentions": {"parse": []}}
    tg = f"<b>{html.escape(head)}</b>\n" + "\n".join(html.escape(b) for b in body)
    return text, discord, _clip(tg, 4000)


NTFY_STYLE = {  # kind -> (priority 1-5, emoji tag)
    "EXIT": (5, "warning"), "VERIFIED": (4, "green_circle"), "UNCONFIRMED": (4, "yellow_circle"),
    "DANGER": (3, "rotating_light"), "UNCHECKED": (3, "grey_question"),
}


def ntfy_payload(topic: str, title: str, body: str, click: str | None, buttons: list[tuple[str, str]],
                 kind: str | None) -> dict:
    """JSON publish body for ntfy (https://docs.ntfy.sh/publish/#publish-as-json).
    Clicking the notification opens `click`; buttons are extra "view" actions."""
    prio, tag = NTFY_STYLE.get(kind or "", (3, "bell"))
    p = {"topic": topic, "title": title[:250], "message": body[:1000], "priority": prio, "tags": [tag]}
    if click and click.startswith(("https://", "http://")):
        p["click"] = click
    actions = [{"action": "view", "label": label, "url": url, "clear": True}
               for label, url in buttons if url and url.startswith(("https://", "http://"))][:3]
    if actions:
        p["actions"] = actions
    return p


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
        self.buy_template = acfg.get("buy_link") or ""
        self.desktop_kinds = set(acfg.get("desktop_on") or ["VERIFIED", "UNCONFIRMED", "DANGER", "UNCHECKED", "EXIT"])
        self.desktop = DesktopNotifier(bool(acfg.get("desktop", False)) and not console_only)
        self.ntfy_server = (acfg.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
        self.use_ntfy = bool(getattr(secrets, "ntfy_topic", "")) and not console_only
        if not console_only and not (self.use_discord or self.use_telegram or self.use_ntfy or self.desktop.enabled):
            log.warning("No alert channel configured (set NTFY_TOPIC, DISCORD_WEBHOOK_URL and/or "
                        "TELEGRAM_BOT_TOKEN+CHAT_ID, or alerts.desktop: true); alerts go to the console only")

    def buy_url_for(self, verdict_label: str, chain: str, address: str) -> str | None:
        return buy_link(self.buy_template, chain, address) if verdict_label in BUYABLE else None

    def should_alert(self, chain: str, address: str, verdict_label: str) -> bool:
        return self.db.last_alert_verdict(chain, address) != verdict_label

    async def send_verdict(self, a: AlertContent) -> bool:
        if not self.should_alert(a.chain, a.address, a.verdict.label):
            log.info("skip alert %s:%s - verdict %s unchanged", a.chain, a.address, a.verdict.label)
            return False
        a.buy_url = self.buy_url_for(a.verdict.label, a.chain, a.address)
        desk = None
        if a.verdict.label in self.desktop_kinds:
            title, body = a.desktop()
            desk = (title, body, click_link(a.verdict.label, self.buy_template, a.chain, a.address, a.dex_url))
        buttons = ([("🛒 Open to buy", a.buy_url)] if a.buy_url else []) + ([("📈 Chart", a.dex_url)] if a.dex_url else [])
        sent = await self._deliver(format_text(a), format_discord(a), format_telegram(a), desk, buttons, a.verdict.label)
        if not sent:
            return False  # not recorded, so the next pass retries
        self.db.record_alert(a.chain, a.address, "verdict", a.verdict.label, sent,
                             {"title": a.title, "reasons": a.verdict.reasons})
        return True

    async def send_exit_warning(self, chain: str, address: str, title: str, flags: list[tuple[str, str]],
                                dex_url: str | None) -> bool:
        text, discord, tg = format_exit_warning(chain, address, title, flags, dex_url)
        desk = None
        if "EXIT" in self.desktop_kinds:
            desk = (f"⚠️ EXIT WARNING {title}", "; ".join(d for _, d in flags)[:200] + ". Click to open the coin.",
                    click_link("EXIT", self.buy_template, chain, address, dex_url))
        sell = buy_link(self.buy_template, chain, address)
        buttons = ([("💸 Open to sell", sell)] if sell else []) + ([("📈 Chart", dex_url)] if dex_url else [])
        sent = await self._deliver(text, discord, tg, desk, buttons, "EXIT")
        if sent:
            self.db.record_alert(chain, address, "exit_warning", None, sent, {"flags": [f for f, _ in flags]})
        return bool(sent)

    async def send_system(self, message: str) -> None:
        text = f"⚙️ corp-coin-watch: {message}"
        await self._deliver(text, {"content": _clip(text, 1900), "allowed_mentions": {"parse": []}}, html.escape(text))
        self.db.record_alert("-", "-", "system", None, [], {"message": message})

    async def _ntfy(self, title: str, body: str, click: str | None, buttons: list[tuple[str, str]],
                    kind: str | None) -> bool:
        headers = {"Authorization": f"Bearer {self.secrets.ntfy_token}"} if getattr(self.secrets, "ntfy_token", "") else {}
        r = await self.http.post_json(self.ntfy_server,
                                      ntfy_payload(self.secrets.ntfy_topic, title, body, click, buttons, kind),
                                      headers=headers, log_name="ntfy")
        if not r.ok:
            log.error("ntfy alert failed: %s", r.error)
        return r.ok

    async def _deliver(self, text: str, discord_payload: dict, telegram_html: str,
                       desktop: tuple[str, str, str | None] | None = None,
                       buttons: list[tuple[str, str]] | None = None, kind: str | None = None) -> list[str]:
        sent: list[str] = []
        if desktop and await self.desktop.send(*desktop):
            sent.append("desktop")
        if desktop and self.use_ntfy and await self._ntfy(*desktop, buttons or [], kind):
            sent.append("ntfy")
        if not (self.use_discord or self.use_telegram):
            print(text, flush=True)  # also lands in the server log (journalctl)
            return sent or ["console"]
        if self.use_discord:
            r = await self.http.post_json(self.secrets.discord_webhook_url, discord_payload,
                                          expect_json=False, log_name="discord-webhook")
            if r.ok:
                sent.append("discord")
            else:
                log.error("Discord alert failed: %s", r.error)
        if self.use_telegram:
            url = f"https://api.telegram.org/bot{self.secrets.telegram_bot_token}/sendMessage"
            payload = {
                "chat_id": self.secrets.telegram_chat_id,
                "text": telegram_html,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            # Tap-able buttons on the phone (Telegram only accepts http/https button links).
            row = [{"text": t, "url": u} for t, u in buttons or [] if u and u.startswith(("https://", "http://"))]
            if row:
                payload["reply_markup"] = {"inline_keyboard": [row]}
            r = await self.http.post_json(url, payload, log_name="telegram-bot")
            if r.ok:
                sent.append("telegram")
            else:
                log.error("Telegram alert failed: %s", r.error)
        return sent
