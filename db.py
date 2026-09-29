"""SQLite state. All timestamps are unix epoch seconds (UTC).

Schema changes go in MIGRATIONS as new entries; PRAGMA user_version tracks
which have run, so existing databases upgrade in place as phases are added.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

MIGRATIONS: list[str] = [
    # 1: phase 1 core tables
    """
    CREATE TABLE seen_items (
        source   TEXT NOT NULL,
        item_id  TEXT NOT NULL,
        seen_at  REAL NOT NULL,
        PRIMARY KEY (source, item_id)
    );

    -- Every time a CA is spotted anywhere. Keyed by address (not chain) because
    -- an EVM address seen in text has no chain until it is resolved.
    CREATE TABLE sightings (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        address     TEXT NOT NULL,
        chain_hint  TEXT,
        kind        TEXT NOT NULL DEFAULT 'token',   -- token | pair
        source      TEXT NOT NULL,                   -- dexscreener | x | telegram | fomo | manual
        source_ref  TEXT NOT NULL,                   -- tweet id, message id, profile url...
        author      TEXT,
        url         TEXT,
        text        TEXT,
        seen_at     REAL NOT NULL,
        UNIQUE (address, source, source_ref)
    );
    CREATE INDEX idx_sightings_address ON sightings(address, seen_at);

    CREATE TABLE tokens (
        chain           TEXT NOT NULL,
        address         TEXT NOT NULL,
        name            TEXT,
        symbol          TEXT,
        pair_address    TEXT,
        dex_url         TEXT,
        links_json      TEXT,
        first_seen_at   REAL NOT NULL,
        updated_at      REAL NOT NULL,
        last_verdict    TEXT,
        PRIMARY KEY (chain, address)
    );

    CREATE TABLE safety_checks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        chain       TEXT NOT NULL,
        address     TEXT NOT NULL,
        checked_at  REAL NOT NULL,
        overall     TEXT NOT NULL,
        report_json TEXT NOT NULL
    );
    CREATE INDEX idx_safety_token ON safety_checks(chain, address, checked_at);

    CREATE TABLE alerts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        chain       TEXT NOT NULL,
        address     TEXT NOT NULL,
        kind        TEXT NOT NULL,       -- verdict | exit_warning | system
        verdict     TEXT,
        sent_at     REAL NOT NULL,
        channels    TEXT,
        payload_json TEXT
    );
    CREATE INDEX idx_alerts_token ON alerts(chain, address, sent_at);

    CREATE TABLE source_health (
        source               TEXT PRIMARY KEY,
        status               TEXT NOT NULL DEFAULT 'unknown',
        last_success_at      REAL,
        last_error_at        REAL,
        last_error           TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        down_alerted         INTEGER NOT NULL DEFAULT 0,
        hour_start           REAL,
        requests_this_hour   INTEGER NOT NULL DEFAULT 0
    );
    """,
]


@dataclass
class Sighting:
    address: str
    source: str
    source_ref: str
    chain_hint: str | None = None
    kind: str = "token"
    author: str | None = None
    url: str | None = None
    text: str | None = None
    seen_at: float | None = None


class DB:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
            with self.conn:
                self.conn.executescript(script)
                self.conn.execute(f"PRAGMA user_version = {i}")

    def close(self) -> None:
        self.conn.close()

    # --- seen items -------------------------------------------------------
    def mark_seen(self, source: str, item_id: str) -> bool:
        """Returns True if this item is new (and marks it seen)."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO seen_items (source, item_id, seen_at) VALUES (?, ?, ?)",
                (source, item_id, time.time()),
            )
        return cur.rowcount == 1

    # --- sightings --------------------------------------------------------
    def add_sighting(self, s: Sighting) -> bool:
        """Records a sighting. Returns False if this exact sighting already exists."""
        with self.conn:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO sightings
                   (address, chain_hint, kind, source, source_ref, author, url, text, seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (s.address, s.chain_hint, s.kind, s.source, s.source_ref, s.author, s.url,
                 (s.text or "")[:2000] or None, s.seen_at or time.time()),
            )
        return cur.rowcount == 1

    def first_sighting(self, address: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sightings WHERE address = ? ORDER BY seen_at, id LIMIT 1", (address,)
        ).fetchone()

    def sightings_for(self, address: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sightings WHERE address = ? ORDER BY seen_at, id", (address,)
        ).fetchall()

    # --- tokens -----------------------------------------------------------
    def upsert_token(self, chain: str, address: str, **fields_: Any) -> None:
        now = time.time()
        first = self.first_sighting(address)
        first_seen = first["seen_at"] if first else now
        links = fields_.pop("links", None)
        cols = {k: v for k, v in fields_.items() if v is not None}
        if links is not None:
            cols["links_json"] = json.dumps(links)
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO tokens (chain, address, first_seen_at, updated_at) VALUES (?, ?, ?, ?)",
                (chain, address, first_seen, now),
            )
            if cols:
                sets = ", ".join(f"{k} = ?" for k in cols)
                self.conn.execute(
                    f"UPDATE tokens SET {sets}, updated_at = ? WHERE chain = ? AND address = ?",
                    (*cols.values(), now, chain, address),
                )

    def get_token(self, chain: str, address: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM tokens WHERE chain = ? AND address = ?", (chain, address)
        ).fetchone()

    # --- safety -----------------------------------------------------------
    def save_safety(self, chain: str, address: str, overall: str, report: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO safety_checks (chain, address, checked_at, overall, report_json) VALUES (?, ?, ?, ?, ?)",
                (chain, address, time.time(), overall, json.dumps(report, default=str)),
            )

    def latest_safety(self, chain: str, address: str, max_age_s: float) -> dict | None:
        row = self.conn.execute(
            """SELECT report_json FROM safety_checks WHERE chain = ? AND address = ? AND checked_at >= ?
               ORDER BY checked_at DESC LIMIT 1""",
            (chain, address, time.time() - max_age_s),
        ).fetchone()
        return json.loads(row["report_json"]) if row else None

    # --- alerts -----------------------------------------------------------
    def last_alert_verdict(self, chain: str, address: str) -> str | None:
        row = self.conn.execute(
            """SELECT verdict FROM alerts WHERE chain = ? AND address = ? AND kind = 'verdict'
               ORDER BY sent_at DESC, id DESC LIMIT 1""",
            (chain, address),
        ).fetchone()
        return row["verdict"] if row else None

    def record_alert(self, chain: str, address: str, kind: str, verdict: str | None,
                     channels: Iterable[str], payload: dict | None = None) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO alerts (chain, address, kind, verdict, sent_at, channels, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (chain, address, kind, verdict, time.time(), ",".join(channels),
                 json.dumps(payload, default=str) if payload else None),
            )

    # --- source health ----------------------------------------------------
    def _ensure_source(self, source: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO source_health (source) VALUES (?)", (source,))

    def count_request(self, source: str, n: int = 1) -> None:
        now = time.time()
        with self.conn:
            self._ensure_source(source)
            row = self.conn.execute("SELECT hour_start FROM source_health WHERE source = ?", (source,)).fetchone()
            if row["hour_start"] is None or now - row["hour_start"] >= 3600:
                self.conn.execute(
                    "UPDATE source_health SET hour_start = ?, requests_this_hour = ? WHERE source = ?",
                    (now, n, source),
                )
            else:
                self.conn.execute(
                    "UPDATE source_health SET requests_this_hour = requests_this_hour + ? WHERE source = ?",
                    (n, source),
                )

    def record_source_ok(self, source: str) -> bool:
        """Marks success. Returns True if the source had been alerted as down (i.e. recovered)."""
        with self.conn:
            self._ensure_source(source)
            was_down = self.conn.execute(
                "SELECT down_alerted FROM source_health WHERE source = ?", (source,)
            ).fetchone()["down_alerted"]
            self.conn.execute(
                """UPDATE source_health SET status = 'ok', last_success_at = ?, consecutive_failures = 0,
                   down_alerted = 0 WHERE source = ?""",
                (time.time(), source),
            )
        return bool(was_down)

    def record_source_error(self, source: str, error: str) -> int:
        """Marks a failure; returns the consecutive failure count."""
        with self.conn:
            self._ensure_source(source)
            self.conn.execute(
                """UPDATE source_health SET status = 'error', last_error_at = ?, last_error = ?,
                   consecutive_failures = consecutive_failures + 1 WHERE source = ?""",
                (time.time(), error[:500], source),
            )
            return self.conn.execute(
                "SELECT consecutive_failures FROM source_health WHERE source = ?", (source,)
            ).fetchone()[0]

    def mark_source_down_alerted(self, source: str) -> bool:
        """Returns True the first time only, so a down source alerts once."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE source_health SET down_alerted = 1 WHERE source = ? AND down_alerted = 0", (source,)
            )
        return cur.rowcount == 1

    def source_health(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM source_health ORDER BY source").fetchall()
