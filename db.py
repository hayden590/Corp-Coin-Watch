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
    # 2: phases 2-7
    """
    ALTER TABLE sightings ADD COLUMN author_id TEXT;
    CREATE INDEX idx_sightings_author ON sightings(author_id, seen_at);
    ALTER TABLE tokens ADD COLUMN deployer TEXT;
    ALTER TABLE tokens ADD COLUMN alerted_at REAL;

    -- X accounts that posted CAs (discovery scorecards) + watched accounts
    CREATE TABLE x_accounts (
        user_id        TEXT PRIMARY KEY,
        handle         TEXT,
        name           TEXT,
        followers      INTEGER,
        statuses       INTEGER,
        verified_type  TEXT,
        website        TEXT,
        created_at     REAL,
        status         TEXT NOT NULL DEFAULT 'normal',   -- normal | promoted | blacklisted
        status_reason  TEXT,
        updated_at     REAL NOT NULL
    );

    CREATE TABLE account_snapshots (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     TEXT NOT NULL,
        taken_at    REAL NOT NULL,
        handle      TEXT, name TEXT, bio TEXT, pfp TEXT, verified_type TEXT,
        statuses    INTEGER, followers INTEGER
    );
    CREATE INDEX idx_snap_user ON account_snapshots(user_id, taken_at);

    CREATE TABLE endorsements (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id   TEXT NOT NULL,
        handle    TEXT,
        tier      INTEGER NOT NULL,
        weight    REAL NOT NULL,
        address   TEXT NOT NULL,
        kind      TEXT NOT NULL,        -- post | reply | quote | retweet
        tweet_id  TEXT NOT NULL,
        at        REAL NOT NULL,
        UNIQUE (user_id, address, tweet_id)
    );
    CREATE INDEX idx_endorse_addr ON endorsements(address);

    -- Tweets from signal/org accounts (narrative + interaction analysis)
    CREATE TABLE tweets (
        tweet_id          TEXT PRIMARY KEY,
        user_id           TEXT NOT NULL,
        text              TEXT,
        created_at        REAL NOT NULL,
        kind              TEXT,
        reply_to_user_id  TEXT,
        quoted_user_id    TEXT,
        retweeted_user_id TEXT
    );
    CREATE INDEX idx_tweets_user ON tweets(user_id, created_at);

    CREATE TABLE pending_checks (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        kind     TEXT NOT NULL,          -- tweet_persistence | dump_check | outcome
        ref      TEXT NOT NULL,
        chain    TEXT, address TEXT,
        due_at   REAL NOT NULL,
        done_at  REAL,
        result   TEXT,
        payload  TEXT,
        UNIQUE (kind, ref, due_at)
    );
    CREATE INDEX idx_pending_due ON pending_checks(done_at, due_at);

    CREATE TABLE website_checks (
        address    TEXT NOT NULL,
        url        TEXT NOT NULL,
        found      INTEGER,
        checked_at REAL NOT NULL,
        PRIMARY KEY (address, url)
    );

    CREATE TABLE follow_edges (
        follower_id TEXT NOT NULL,
        followee_id TEXT NOT NULL,
        fetched_at  REAL NOT NULL,
        PRIMARY KEY (follower_id, followee_id)
    );
    CREATE INDEX idx_follow_followee ON follow_edges(followee_id);
    CREATE TABLE graph_refresh (user_id TEXT PRIMARY KEY, refreshed_at REAL NOT NULL);

    CREATE TABLE wallets (
        wallet   TEXT NOT NULL,
        chain    TEXT NOT NULL,
        label    TEXT,
        source   TEXT NOT NULL,           -- manual | fomo | signal
        owner_user_id TEXT,
        last_polled REAL,
        PRIMARY KEY (wallet, chain)
    );
    CREATE TABLE wallet_activity (
        wallet  TEXT NOT NULL,
        chain   TEXT NOT NULL,
        token   TEXT NOT NULL,
        side    TEXT NOT NULL,            -- buy | sell
        amount  REAL,
        at      REAL NOT NULL,
        tx      TEXT NOT NULL,
        UNIQUE (tx, wallet, token, side)
    );
    CREATE INDEX idx_wact_token ON wallet_activity(token, at);
    CREATE INDEX idx_wact_wallet ON wallet_activity(wallet, at);

    CREATE TABLE fee_checks (
        chain TEXT NOT NULL, address TEXT NOT NULL, checked_at REAL NOT NULL, result_json TEXT NOT NULL
    );
    CREATE INDEX idx_fee_token ON fee_checks(chain, address, checked_at);

    CREATE TABLE market_snapshots (
        chain TEXT NOT NULL, address TEXT NOT NULL, taken_at REAL NOT NULL,
        price_usd REAL, liquidity_usd REAL, volume_h1 REAL, buys_h1 INTEGER, sells_h1 INTEGER,
        holder_count INTEGER, top10_pct REAL
    );
    CREATE INDEX idx_msnap_token ON market_snapshots(chain, address, taken_at);

    CREATE TABLE chart_snapshots (
        chain TEXT NOT NULL, address TEXT NOT NULL, taken_at REAL NOT NULL, features_json TEXT NOT NULL
    );
    CREATE INDEX idx_csnap_token ON chart_snapshots(chain, address, taken_at);

    CREATE TABLE exit_flags (
        chain TEXT NOT NULL, address TEXT NOT NULL, flag TEXT NOT NULL, detail TEXT, at REAL NOT NULL,
        alerted INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chain, address, flag)
    );

    CREATE TABLE text_scores (
        chain TEXT NOT NULL, address TEXT NOT NULL, scored_at REAL NOT NULL, result_json TEXT NOT NULL
    );
    CREATE INDEX idx_text_token ON text_scores(chain, address, scored_at);

    CREATE TABLE fomo_theses (
        address TEXT NOT NULL, author TEXT, text TEXT NOT NULL, fetched_at REAL NOT NULL,
        UNIQUE (address, author, text)
    );

    -- Backtesting: one feature snapshot per token at first full assessment
    CREATE TABLE feature_snapshots (
        chain TEXT NOT NULL, address TEXT NOT NULL, taken_at REAL NOT NULL,
        first_seen_at REAL NOT NULL, entry_price REAL, verdict TEXT,
        features_json TEXT NOT NULL,
        PRIMARY KEY (chain, address)
    );
    CREATE TABLE outcomes (
        chain TEXT NOT NULL, address TEXT NOT NULL, horizon TEXT NOT NULL,
        recorded_at REAL NOT NULL, max_gain REAL, max_drawdown REAL, final_return REAL,
        rugged INTEGER, path_json TEXT,
        PRIMARY KEY (chain, address, horizon)
    );

    CREATE TABLE paper_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy TEXT NOT NULL, chain TEXT NOT NULL, address TEXT NOT NULL,
        opened_at REAL NOT NULL, entry_price REAL NOT NULL, stake REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        closed_at REAL, exit_price REAL, exit_reason TEXT, pnl_pct REAL,
        peak_price REAL,
        UNIQUE (strategy, chain, address)
    );
    """,
    # 3: early buyers noted while a rising coin is still young (cheap to fetch then), for wallet discovery
    """
    CREATE TABLE early_buyers (
        chain TEXT NOT NULL, token TEXT NOT NULL, wallet TEXT NOT NULL, at REAL NOT NULL, tx TEXT,
        PRIMARY KEY (chain, token, wallet)
    );
    CREATE INDEX idx_sightings_source ON sightings(source, seen_at);
    """,
    # 4: coins the user says they are in (exit warnings only go out for these)
    """
    CREATE TABLE holdings (
        chain TEXT NOT NULL, address TEXT NOT NULL, opened_at REAL NOT NULL, closed_at REAL,
        PRIMARY KEY (chain, address)
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
    author_id: str | None = None


class DB:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
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
                   (address, chain_hint, kind, source, source_ref, author, url, text, seen_at, author_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (s.address, s.chain_hint, s.kind, s.source, s.source_ref, s.author, s.url,
                 (s.text or "")[:2000] or None, s.seen_at or time.time(), s.author_id),
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

    # --- generic helpers --------------------------------------------------
    def q(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def q1(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def x(self, sql: str, params: tuple | list = ()) -> int:
        with self.conn:
            return self.conn.execute(sql, params).rowcount

    # --- pending checks (scheduled re-checks) ------------------------------
    def schedule(self, kind: str, ref: str, due_at: float, chain: str | None = None,
                 address: str | None = None, payload: dict | None = None) -> bool:
        return self.x(
            """INSERT OR IGNORE INTO pending_checks (kind, ref, chain, address, due_at, payload)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (kind, ref, chain, address, due_at, json.dumps(payload) if payload else None),
        ) == 1

    def due_checks(self, now: float | None = None, limit: int = 50) -> list[sqlite3.Row]:
        return self.q(
            "SELECT * FROM pending_checks WHERE done_at IS NULL AND due_at <= ? ORDER BY due_at LIMIT ?",
            (now or time.time(), limit),
        )

    def complete_check(self, check_id: int, result: str) -> None:
        self.x("UPDATE pending_checks SET done_at = ?, result = ? WHERE id = ?", (time.time(), result, check_id))

    def checks_for(self, kind: str, address: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM pending_checks WHERE kind = ? AND address = ? ORDER BY due_at", (kind, address))
