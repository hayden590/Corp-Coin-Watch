"""corp-coin-watch - finds new meme-coin CAs, checks them, alerts you.

HARD RULE: this program never buys or sells anything. There is no wallet key,
no signing code, and no trading code path. Alerts and paper trading only.

Usage:
  python main.py [run]              run the live watcher (all sources)
  python main.py --dry-run          full pipeline on sample_data/, no keys or network
  python main.py check "<text|CA>"  check one CA / message now (add --send to alert)
  python main.py health             status of every source, X account and the AI backend
  python main.py leaderboard        best and worst X accounts that post CAs
  python main.py backtest           replay strategies on recorded outcomes
  python main.py qualify            paper-trading report card ("NO EDGE FOUND" if it fails)
  python main.py new-ntfy-topic     make a private channel for phone + laptop pop-ups (ntfy)
  python main.py test-notify        send a sample alert (click it to test the link)
  python main.py x-login            add X burner accounts from .env to twscrape
  python main.py telegram-login     log in to Telegram once (creates the local session)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import ROOT, SecretRedactor, Secrets, load_config
from db import DB
from net import Http
from pipeline import Pipeline

log = logging.getLogger("corp-coin-watch")

X_DB = ROOT / "data" / "x_accounts.db"
TG_SESSION = ROOT / "data" / "telegram"
MODEL_PATH = ROOT / "data" / "model.pkl"


def setup_logging(cfg: dict, secrets: Secrets, to_file: bool = True) -> None:
    lcfg = cfg["logging"]
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, str(lcfg.get("level", "INFO")).upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if to_file:
        path = ROOT / lcfg["file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=int(lcfg["max_bytes"]),
                                            backupCount=int(lcfg["backups"]), encoding="utf-8"))
    redactor = SecretRedactor(secrets)
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(redactor)
        root.addHandler(h)
    # httpx logs full URLs at INFO (Telegram bot token / Helius key live in URLs): keep it quiet.
    for noisy in ("httpx", "httpcore", "httpx2", "twscrape", "telethon", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def make_http(cfg: dict, db: DB | None, transport=None) -> Http:
    return Http(cfg["rate_limits"], transport=transport,
                on_request=(lambda host: db.count_request(host)) if db else None)


def make_x_client(cfg: dict, db: DB):
    if not X_DB.exists():
        return None
    try:
        from sources.x_source import TwscrapeClient

        return TwscrapeClient(X_DB, int(cfg["x"].get("requests_per_hour", 150)), on_request=db.count_request)
    except ImportError:
        log.warning("twscrape not installed - X source disabled")
        return None


async def run_live(cfg: dict, secrets: Secrets) -> None:
    from monitor import Monitor
    from sources.fomo_source import FomoSource
    from sources.telegram_source import TelegramSource
    from sources.x_source import XSource

    db = DB(ROOT / cfg["db_path"])
    http = make_http(cfg, db)
    x_client = make_x_client(cfg, db)
    pipe = Pipeline(cfg, db, http, secrets, x_client=x_client, model_path=MODEL_PATH)
    telegram = TelegramSource(secrets.telegram_api_id, secrets.telegram_api_hash, TG_SESSION, cfg.get("channels") or [])
    monitor = Monitor(pipe, cfg, XSource(x_client, db, cfg) if x_client else None, telegram,
                      FomoSource(http, db, cfg, secrets.fomo_api_key))
    log.info("corp-coin-watch started: chains=%s. Alerts and paper trading only - never trades real money.",
             ",".join(cfg["chains"]))
    try:
        await monitor.run()
    finally:
        await telegram.stop()
        await http.aclose()
        db.close()


async def run_check(cfg: dict, secrets: Secrets, text: str, send: bool) -> int:
    db = DB(ROOT / cfg["db_path"])
    http = make_http(cfg, db)
    pipe = Pipeline(cfg, db, http, secrets, console_only=not send, x_client=make_x_client(cfg, db),
                    model_path=MODEL_PATH)
    try:
        results = await pipe.process_text(text, "manual", f"cli-{int(time.time())}")
    finally:
        await http.aclose()
        db.close()
    if not results:
        print("No contract address found in that text.")
        return 1
    for r in results:
        if r.status in ("no_pair", "resolve_failed"):
            print(f"{r.address}: {r.status.replace('_', ' ')}")
        elif r.status == "not_alerted" and r.verdict:
            print(f"{r.address}: {r.verdict.label} ({r.reason})")
    return 0


async def show_health(cfg: dict, secrets: Secrets) -> int:
    path = ROOT / cfg["db_path"]
    db = DB(path)
    now = time.time()

    def ts(v):
        if not v:
            return "never"
        return datetime.fromtimestamp(v, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") + f" ({(now - v) / 60:.0f}m ago)"

    rows = db.source_health()
    print(f"{'source / host':<28} {'status':<8} {'req/hr':>6}  last success")
    for r in rows:
        in_hour = r["hour_start"] and now - r["hour_start"] < 3600
        reqs = r["requests_this_hour"] if in_hour else 0
        print(f"{r['source']:<28} {r['status']:<8} {reqs:>6}  {ts(r['last_success_at'])}")
        if r["status"] == "error" and r["last_error"]:
            print(f"{'':<28} last error: {r['last_error']} ({r['consecutive_failures']} in a row)")
    if not rows:
        print("(no activity recorded yet)")

    print("\nX (twscrape):")
    x = make_x_client(cfg, db)
    if not x:
        print("  not configured - put accounts in .env and run: python main.py x-login")
    else:
        st = await x.status()
        for a in st["accounts"]:
            print(f"  @{a['username']:<20} active={a['active']} logged_in={a['logged_in']} "
                  f"requests={a['requests']} {a['error'] or ''}")
        print(f"  hourly budget: {cfg['x'].get('requests_per_hour')} requests")

    last_tg = db.q1("SELECT MAX(seen_at) AS t FROM sightings WHERE source = 'telegram'")["t"]
    tg_ok = Path(str(TG_SESSION) + ".session").exists()
    print(f"\nTelegram: session {'present' if tg_ok else 'missing (run telegram-login)'}, "
          f"{len(cfg.get('channels') or [])} channel(s), last CA message {ts(last_tg)}")
    print(f"Helius: {'key set' if secrets.helius_api_key else 'no key (Solana wallets + global fees disabled)'}")
    fomo_url = (cfg.get("fomo") or {}).get("leaderboard_url")
    print(f"Fomo: {'endpoint ' + fomo_url if fomo_url else 'fallback mode (fomo_wallets.yaml) - fomo.family has no public API'}")
    http = make_http(cfg, None)
    from text_analysis import TextAnalyzer

    print(f"AI backend: {await TextAnalyzer(db, http, cfg, secrets.anthropic_api_key).status()}")
    await http.aclose()
    from desktop_notify import DesktopNotifier

    desk = DesktopNotifier(bool(cfg["alerts"].get("desktop"))).backend()[1] if cfg["alerts"].get("desktop") else "off"
    print(f"Desktop pop-ups: {desk}; click opens: {cfg['alerts'].get('buy_link') or 'DexScreener'}")
    print("Alert channels:",
          ", ".join(n for n, ok in (("discord", secrets.discord_webhook_url),
                                    ("telegram", secrets.telegram_bot_token and secrets.telegram_chat_id)) if ok)
          or "none configured (console only)")
    open_trades = db.q1("SELECT COUNT(*) AS n FROM paper_trades WHERE status = 'open'")["n"]
    pending = db.q1("SELECT COUNT(*) AS n FROM pending_checks WHERE done_at IS NULL")["n"]
    print(f"Paper trades open: {open_trades} | scheduled checks pending: {pending}")
    db.close()
    return 0


async def test_notify(cfg: dict, secrets: Secrets) -> int:
    """Send one sample alert through ntfy (phone + laptop) and/or this computer's pop-ups."""
    from alerts import Alerter
    from desktop_notify import click_link

    sample = "6ce9TvjRyG4XEwjEcm16AXyf2hxrXtCEsth429Uk7MwU"
    dex = f"https://dexscreener.com/solana/{sample}"
    url = click_link("UNCONFIRMED", cfg["alerts"].get("buy_link"), "solana", sample, dex)
    title, body = "🟡 UNCONFIRMED TEST ($TEST)", "Test from corp-coin-watch. Click to open the link."
    db = DB(":memory:")
    http = make_http(cfg, None)
    al = Alerter(http, secrets, cfg, db)
    ok = False
    try:
        if al.use_ntfy:
            ok = await al._ntfy(title, body, url, [("🛒 Open to buy", url), ("📈 Chart", dex)], "UNCONFIRMED")
            print(f"ntfy: {'sent to your topic' if ok else 'FAILED - check NTFY_TOPIC / network'}")
        else:
            print("ntfy: not set up (NTFY_TOPIC is empty in .env)")
        backend, status = al.desktop.backend()
        if cfg["alerts"].get("desktop") and backend:
            ok = await al.desktop.send(title, body, url) or ok
        print(f"This computer's pop-ups: {status if cfg['alerts'].get('desktop') else 'off (alerts.desktop: false)'}")
    finally:
        await http.aclose()
        db.close()
    if ok:
        print(f"Clicking the notification should open: {url}")
    return 0 if ok else 1


def new_ntfy_topic() -> int:
    import secrets as pysecrets

    topic = "ccw-" + pysecrets.token_urlsafe(18).replace("_", "x").replace("-", "y")
    print(f"""Your private alert channel name (keep it secret - it works like a password):

    {topic}

1. Put it in .env on the machine running the bot:   NTFY_TOPIC={topic}
2. Laptop, no install: open https://ntfy.sh/app -> "Subscribe to topic" -> {topic}
   -> allow notifications. Or run the native listener: python laptop_notifier.py {topic}
3. Phone: install the "ntfy" app (App Store / Google Play) -> + -> {topic}
4. Test it:  python main.py test-notify""")
    return 0


def cmd_backtest(cfg: dict, db: DB) -> dict:
    from backtest import engine

    rep = engine.run(db, cfg, MODEL_PATH)
    print(engine.format_report(rep))
    return rep


def cmd_qualify(cfg: dict, db: DB) -> int:
    from backtest import engine
    from papertrade import qualify

    rep = engine.run(db, cfg, MODEL_PATH)  # also retrains the optional ML model on the newest data
    ok, text = qualify(db, cfg, rep)
    print(text)
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="corp-coin-watch", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="run on sample data, no keys or network")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run", help="run the live watcher (default)")
    c = sub.add_parser("check", help="check CAs in a piece of text")
    c.add_argument("text")
    c.add_argument("--send", action="store_true", help="also send the alert to Discord/Telegram")
    sub.add_parser("health", help="show source status")
    lb = sub.add_parser("leaderboard", help="best and worst CA-posting X accounts")
    lb.add_argument("--min-calls", type=int, default=1)
    sub.add_parser("backtest", help="backtest strategies on recorded outcomes")
    sub.add_parser("qualify", help="paper trading report card")
    sub.add_parser("test-notify", help="send a sample alert to ntfy / this computer's pop-ups")
    sub.add_parser("new-ntfy-topic", help="make a private ntfy channel name for phone + laptop alerts")
    sub.add_parser("x-login", help="register X burner accounts from .env")
    sub.add_parser("telegram-login", help="log in to Telegram (interactive, once)")
    args = p.parse_args(argv)

    cfg = load_config()
    if args.dry_run:
        from dryrun import run_dry

        setup_logging(cfg, Secrets(), to_file=False)
        return asyncio.run(run_dry(cfg))
    secrets = Secrets.from_env()
    setup_logging(cfg, secrets, to_file=args.cmd in (None, "run", "check"))
    if args.cmd == "health":
        return asyncio.run(show_health(cfg, secrets))
    if args.cmd in ("leaderboard", "backtest", "qualify"):
        db = DB(ROOT / cfg["db_path"])
        try:
            if args.cmd == "leaderboard":
                from discovery import Discovery

                print(Discovery(db, cfg).leaderboard(args.min_calls))
                return 0
            if args.cmd == "backtest":
                cmd_backtest(cfg, db)
                return 0
            return cmd_qualify(cfg, db)
        finally:
            db.close()
    if args.cmd == "new-ntfy-topic":
        return new_ntfy_topic()
    if args.cmd == "test-notify":
        return asyncio.run(test_notify(cfg, secrets))
    if args.cmd == "x-login":
        from sources.x_source import add_accounts_from_env

        if not (secrets.x_accounts or secrets.x_cookies):
            print("Set X_ACCOUNTS and/or X_COOKIES in .env first (see .env.example).")
            return 1
        X_DB.parent.mkdir(parents=True, exist_ok=True)
        n = asyncio.run(add_accounts_from_env(X_DB, secrets.x_accounts, secrets.x_cookies))
        print(f"Registered {n} X account(s). Check them with: python main.py health")
        return 0
    if args.cmd == "telegram-login":
        from sources.telegram_source import TelegramSource

        if not (secrets.telegram_api_id and secrets.telegram_api_hash):
            print("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first (https://my.telegram.org -> API tools).")
            return 1
        asyncio.run(TelegramSource(secrets.telegram_api_id, secrets.telegram_api_hash, TG_SESSION, []).login())
        return 0
    if args.cmd == "check":
        return asyncio.run(run_check(cfg, secrets, args.text, args.send))
    try:
        asyncio.run(run_live(cfg, secrets))
    except KeyboardInterrupt:
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
