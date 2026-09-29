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
    "poll": {"dexscreener_seconds": 90, "jitter_pct": 20},
    "safety": {
        "min_liquidity_usd": 10000,
        "max_top10_holder_pct": 30,
        "max_buy_tax_pct": 10,
        "max_sell_tax_pct": 10,
        "min_lp_locked_pct": 80,
        "warn_pair_age_minutes": 10,
        "recheck_minutes": 30,
    },
    "alerts": {
        "discord": True,
        "telegram": True,
        "danger_alert_sources": ["x", "telegram", "fomo", "manual"],
        "source_down_after_failures": 5,
    },
    "rate_limits": {
        "api.dexscreener.com": 50,
        "api.rugcheck.xyz": 30,
        "api.gopluslabs.io": 20,
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
    """Merge config.yaml over DEFAULTS and attach the optional list files."""
    base = Path(config_dir) if config_dir else ROOT
    cfg = deep_merge(DEFAULTS, _load_yaml(base / "config.yaml"))
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
