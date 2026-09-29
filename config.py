"""Config loading. Every file is optional; built-in defaults cover everything.

Secrets come only from the environment / .env and are never logged: the
Secrets object hides values in repr(), and `SecretRedactor` scrubs any secret
value that slips into a log line.
"""
from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent

DEFAULTS: dict[str, Any] = {
    "chains": ["solana", "ethereum", "base", "bsc"],
    "poll": {
        "dexscreener_seconds": 30,
        "x_search_min_seconds": 180,
        "x_search_max_seconds": 300,
        "x_watch_seconds": 240,
        "monitor_seconds": 300,
        "slow_minutes": 20,
        "fomo_hours": 6,
        "jitter_pct": 20,
    },
    "safety": {
        "min_liquidity_usd": 10000,
        "max_top10_holder_pct": 30,
        "max_buy_tax_pct": 10,
        "max_sell_tax_pct": 10,
        "min_lp_locked_pct": 80,
        "warn_pair_age_minutes": 10,
        "recheck_minutes": 30,
    },
    "fees": {
        "enabled": True,
        "min_global_fees_sol": 1.5,
        "min_fees_to_volume": 0.001,
        "wash_min_volume_usd": 20000,
        "max_pages": 5,
        "recheck_minutes": 5,
    },
    "x": {
        "queries": ["pump.fun", "dexscreener.com", "CA:", "contract address"],
        "requests_per_hour": 150,
        "search_limit": 20,
        "watch_limit": 20,
        "snapshot_hours": 24,
        "no_account_cooldown_minutes": 15,
        "pace": 1.0,
    },
    "verify": {
        "persistence_delay_minutes": 15,
        "integrity_window_hours": 72,
        "integrity_fields": ["handle", "name", "bio", "pfp", "verified_type"],
        "website_paths": ["", "/press", "/news", "/token", "/crypto"],
        "website_cache_hours": 6,
        "narrative_window_hours": 48,
    },
    "discovery": {
        "promote_min_calls": 5,
        "promote_min_good_pct": 40,
        "promote_max_rug_pct": 20,
        "blacklist_min_calls": 3,
        "blacklist_min_rug_pct": 50,
    },
    "wallets": {
        "poll_minutes": 15,
        "poll_batch": 10,
        "min_history": 10,
        "baseline_win_rate": 0.3,
        "dump_window_minutes": 60,
    },
    "graph": {
        "tier1_refresh_days": 3,
        "tier2_refresh_days": 7,
        "top_tier2": 50,
        "max_following": 500,
        "interaction_days": 90,
        "follower_sample": 100,
        "rename_days": 30,
        "hijack_is_danger": True,
        "cache_hours": 12,
        "serial_launcher_min": 10,
        "deployer_rug_flag": 2,
        "pumpfun_api": "https://frontend-api-v3.pump.fun",
    },
    "charts": {
        "liquidity_drop_pct": 50,
        "top10_drop_points": 5,
        "holder_drop_pct": 10,
        "buy_fade_pct": 40,
    },
    "text": {
        "backend": "none",
        "claude_model": "claude-haiku-4-5",
        "ollama_url": "http://localhost:11434",
        "ollama_model": "llama3.1:8b",
        "max_texts": 40,
        "recheck_minutes": 30,
        "bot_duplicate_ratio": 0.5,
    },
    "fomo": {
        "leaderboard_url": "",
        "theses_url": "",
        "max_wallets": 50,
    },
    "scoring": {
        "tier1_weight": 3.0,
        "tier2_weight": 1.5,
        "promoted_weight": 1.0,
        "channel_weight": 1.0,
        "x_poster_weight": 0.2,
        "x_poster_cap": 1.0,
        "blacklisted_penalty": -1.0,
        "smart_wallet_weight": 1.0,
        "fomo_thesis_weight": 0.1,
        "tier1_interaction": 2.0,
        "tier2_interaction": 0.5,
        "tier1_follow": 0.5,
        "tier2_follow": 0.1,
        "tier2_follow_cap": 1.0,
        "recency_days": 30,
        "fake_followers_penalty": -0.5,
        "connection_weight": 1.0,
        "chart_weight": 1.0,
        "text_weight": 0.5,
        "narrative_penalty": -1.0,
        "deployer_penalty": -1.0,
    },
    "alerts": {
        "discord": True,
        "telegram": True,
        "danger_alert_sources": ["x", "telegram", "fomo", "manual"],
        "min_backing_to_alert": 1.0,
        "desktop": True,
        "desktop_on": ["VERIFIED", "UNCONFIRMED", "DANGER", "UNCHECKED", "EXIT"],
        "buy_link": "",
        "exit_warnings": True,
        "monitor_hours": 48,
        "source_down_after_failures": 5,
    },
    "backtest": {
        "take_profit_pct": 50,
        "stop_loss_pct": 30,
        "fee_pct": 1.0,
        "slippage_pct": 2.0,
        "train_frac": 0.7,
        "stake_pct": 5,
    },
    "strategies": [
        {"name": "backed", "min_verdict": "UNCONFIRMED", "min_backing": 1.5,
         "take_profit_pct": 50, "stop_loss_pct": 30, "max_hold_hours": 24, "exit_on_flags": True},
        {"name": "verified_only", "min_verdict": "VERIFIED",
         "take_profit_pct": 100, "stop_loss_pct": 35, "max_hold_hours": 24, "exit_on_flags": True},
    ],
    "paper": {"stake": 50},
    "qualify": {
        "min_trades": 100,
        "min_days": 21,
        "min_ev_pct": 0.0,
        "max_drawdown_pct": 30,
    },
    "rate_limits": {
        "api.dexscreener.com": 50,
        "api.rugcheck.xyz": 30,
        "api.gopluslabs.io": 20,
        "api.geckoterminal.com": 25,
        "public-api.birdeye.so": 30,
        "api.helius.xyz": 60,
        "eth.blockscout.com": 20,
        "base.blockscout.com": 20,
        "api.etherscan.io": 20,
        "frontend-api-v3.pump.fun": 20,
        "discord.com": 20,
        "api.telegram.org": 20,
    },
    "logging": {
        "level": "INFO",
        "file": "logs/corp-coin-watch.log",
        "max_bytes": 5_000_000,
        "backups": 5,
    },
    "db_path": "data/corp-coin-watch.db",
}

# Optional override files and the top-level key each one holds.
OVERRIDE_FILES = {
    "accounts": ("accounts.yaml", "accounts"),
    "signals": ("signals.yaml", "signals"),
    "channels": ("channels.yaml", "channels"),
    "smart_wallets": ("smart_wallets.yaml", "wallets"),
    "fomo_wallets": ("fomo_wallets.yaml", "wallets"),
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        logging.getLogger(__name__).error("Ignoring invalid YAML in %s: %s", path.name, exc)
        return {}
    return data if isinstance(data, dict) else {}


def load_config(config_dir: Path | str | None = None) -> dict[str, Any]:
    """Merge config.yaml (then this machine's config.local.yaml) over DEFAULTS and
    attach the optional list files."""
    base = Path(config_dir) if config_dir else ROOT
    cfg = deep_merge(DEFAULTS, _load_yaml(base / "config.yaml"))
    cfg = deep_merge(cfg, _load_yaml(base / "config.local.yaml"))  # per-machine, gitignored
    for name, (filename, key) in OVERRIDE_FILES.items():
        data = _load_yaml(base / filename)
        cfg[name] = data.get(key) or []
        if name == "signals":
            cfg["signals_force_include"] = data.get("force_include") or []
            cfg["signals_force_block"] = data.get("force_block") or []
    cfg["chains"] = [c.lower() for c in cfg.get("chains") or []]
    return cfg


def _secret_field() -> Any:
    return field(default="", repr=False)


@dataclass
class Secrets:
    discord_webhook_url: str = _secret_field()
    telegram_bot_token: str = _secret_field()
    telegram_chat_id: str = _secret_field()
    telegram_api_id: str = _secret_field()
    telegram_api_hash: str = _secret_field()
    x_accounts: str = _secret_field()
    x_cookies: str = _secret_field()
    helius_api_key: str = _secret_field()
    anthropic_api_key: str = _secret_field()
    etherscan_api_key: str = _secret_field()
    birdeye_api_key: str = _secret_field()
    fomo_api_key: str = _secret_field()

    @classmethod
    def from_env(cls, load_dotenv_file: bool = True) -> "Secrets":
        if load_dotenv_file:
            from dotenv import load_dotenv

            load_dotenv(ROOT / ".env")
        return cls(**{f.name: os.getenv(f.name.upper(), "").strip() for f in fields(cls)})

    def values(self) -> list[str]:
        return [getattr(self, f.name) for f in fields(self) if getattr(self, f.name)]

    def __repr__(self) -> str:  # never print values
        present = [f.name for f in fields(self) if getattr(self, f.name)]
        return f"Secrets(set={present})"

    __str__ = __repr__


class SecretRedactor(logging.Filter):
    """Replaces any secret value appearing in a log message with ***."""

    def __init__(self, secrets: Secrets):
        super().__init__()
        # Only redact strings long enough not to mangle ordinary text.
        self._values = sorted((v for v in secrets.values() if len(v) >= 6), key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._values:
            return True
        msg = record.getMessage()
        redacted = msg
        for v in self._values:
            redacted = redacted.replace(v, "***")
        if redacted != msg:
            record.msg, record.args = redacted, None
        return True
