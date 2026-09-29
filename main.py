"""corp-coin-watch - finds new meme-coin CAs, checks them, alerts you.

HARD RULE: this program never buys or sells anything. There is no wallet key,
no signing code, and no trading code path. Alerts and paper trading only.

Usage:
  python main.py [run]              run the live watcher
  python main.py --dry-run          full pipeline on sample_data/, no keys or network
  python main.py check "<text|CA>"  check one CA / message now (add --send to alert)
  python main.py health             source status and request counts
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import random
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
    # httpx logs full URLs at INFO (Telegram bot token is in the URL path): keep it quiet.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def make_http(cfg: dict, db: DB | None, transport=None) -> Http:
    return Http(cfg["rate_limits"], transport=transport,
                on_request=(lambda host: db.count_request(host)) if db else None)


async def run_live(cfg: dict, secrets: Secrets) -> None:
    db = DB(ROOT / cfg["db_path"])
    http = make_http(cfg, db)
    pipe = Pipeline(cfg, db, http, secrets)
    interval = float(cfg["poll"]["dexscreener_seconds"])
    jitter = float(cfg["poll"].get("jitter_pct", 20)) / 100
    log.info("corp-coin-watch started: chains=%s, dex poll ~%ss. Alerts only - never trades.",
             ",".join(cfg["chains"]), int(interval))
    try:
        while True:
            try:
                results = await pipe.poll_dex()
                alerted = sum(r.status == "alerted" for r in results)
                log.info("dex poll: %d new tokens, %d alerted", len(results), alerted)
            except Exception:  # one bad cycle must never kill the watcher
                log.exception("dex poll cycle crashed; continuing")
            await asyncio.sleep(interval * random.uniform(1 - jitter, 1 + jitter))
    finally:
        await http.aclose()
        db.close()


async def run_dry(cfg: dict) -> int:
    from dryrun import SampleTransport, sample_messages

    db = DB(":memory:")
    transport = SampleTransport()
    cfg = {**cfg, "rate_limits": {host: 100_000 for host in cfg["rate_limits"]}}  # mocked, no need to pace
    http = make_http(cfg, db, transport)
    http.base_backoff = 0.01
    pipe = Pipeline(cfg, db, http, Secrets(), console_only=True)
    print("DRY RUN - sample data only, nothing is sent anywhere.\n")
    try:
        results = await pipe.poll_dex()
        for m in sample_messages():
            results += await pipe.process_text(m["text"], m["source"], m["source_ref"], m.get("author"))
    finally:
        await http.aclose()
    print("\nSummary:")
    for r in results:
        label = r.verdict.label if r.verdict else "-"
        print(f"  {r.status:<14} {label:<12} {r.chain or '?':<9} {r.address}")
    print(f"\n{len(transport.calls)} mocked API calls. DB rows: "
          f"{db.conn.execute('SELECT COUNT(*) FROM sightings').fetchone()[0]} sightings, "
          f"{db.conn.execute('SELECT COUNT(*) FROM alerts').fetchone()[0]} alerts.")
    db.close()
    return 0


async def run_check(cfg: dict, secrets: Secrets, text: str, send: bool) -> int:
    db = DB(ROOT / cfg["db_path"])
    http = make_http(cfg, db)
    pipe = Pipeline(cfg, db, http, secrets, console_only=not send)
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
            print(f"{r.address}: {r.verdict.label} (already alerted with this verdict)")
    return 0


def show_health(cfg: dict) -> int:
    path = ROOT / cfg["db_path"]
    if not Path(path).exists():
        print("No database yet - run the watcher first.")
        return 1
    db = DB(path)
    rows = db.source_health()
    if not rows:
        print("No source activity recorded yet.")
    now = time.time()

    def ts(v):
        if not v:
            return "never"
        return datetime.fromtimestamp(v, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") + f" ({(now - v) / 60:.0f}m ago)"

    print(f"{'source':<22} {'status':<8} {'req/hr':>6}  last success")
    for r in rows:
        in_hour = r["hour_start"] and now - r["hour_start"] < 3600
        reqs = r["requests_this_hour"] if in_hour else 0
        print(f"{r['source']:<22} {r['status']:<8} {reqs:>6}  {ts(r['last_success_at'])}")
        if r["status"] == "error" and r["last_error"]:
            print(f"{'':<22} last error: {r['last_error']} ({r['consecutive_failures']} in a row)")
    secrets = Secrets.from_env()
    print("\nAlert channels:",
          ", ".join(n for n, ok in (("discord", secrets.discord_webhook_url),
                                    ("telegram", secrets.telegram_bot_token and secrets.telegram_chat_id)) if ok)
          or "none configured (console only)")
    print("Phase 1: X, Telegram, Fomo sources and the AI backend are not built yet.")
    db.close()
    return 0


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
    args = p.parse_args(argv)

    cfg = load_config()
    if args.dry_run:
        setup_logging(cfg, Secrets(), to_file=False)
        return asyncio.run(run_dry(cfg))
    secrets = Secrets.from_env()
    if args.cmd == "health":
        return show_health(cfg)
    setup_logging(cfg, secrets)
    if args.cmd == "check":
        return asyncio.run(run_check(cfg, secrets, args.text, args.send))
    try:
        asyncio.run(run_live(cfg, secrets))
    except KeyboardInterrupt:
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
