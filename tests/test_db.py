from db import DB, MIGRATIONS, Sighting


def test_migrate_idempotent(tmp_path):
    path = tmp_path / "t.db"
    DB(path).close()
    db = DB(path)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_sighting_dedupe_and_first_seen():
    db = DB()
    assert db.add_sighting(Sighting("A", "x", "t1", seen_at=200))
    assert not db.add_sighting(Sighting("A", "x", "t1", seen_at=300))
    assert db.add_sighting(Sighting("A", "telegram", "m1", seen_at=100))
    first = db.first_sighting("A")
    assert first["source"] == "telegram" and first["seen_at"] == 100
    assert len(db.sightings_for("A")) == 2


def test_mark_seen():
    db = DB()
    assert db.mark_seen("dex", "1") and not db.mark_seen("dex", "1")


def test_token_upsert_keeps_first_seen():
    db = DB()
    db.add_sighting(Sighting("A", "x", "t1", seen_at=50))
    db.upsert_token("solana", "A", name="Cat", links={"x": ["u"]})
    db.upsert_token("solana", "A", symbol="CAT", last_verdict="DANGER")
    t = db.get_token("solana", "A")
    assert (t["name"], t["symbol"], t["first_seen_at"], t["last_verdict"]) == ("Cat", "CAT", 50, "DANGER")


def test_alert_history_last_verdict():
    db = DB()
    assert db.last_alert_verdict("solana", "A") is None
    db.record_alert("solana", "A", "verdict", "UNCONFIRMED", ["discord"])
    db.record_alert("solana", "A", "verdict", "DANGER", ["discord"])
    assert db.last_alert_verdict("solana", "A") == "DANGER"


def test_source_down_alerts_once_and_recovers():
    db = DB()
    for _ in range(3):
        db.record_source_error("dex", "boom")
    assert db.mark_source_down_alerted("dex")
    assert not db.mark_source_down_alerted("dex")
    assert db.record_source_ok("dex") is True  # recovered
    assert db.record_source_ok("dex") is False


def test_request_counter():
    db = DB()
    db.count_request("api.x")
    db.count_request("api.x", 2)
    assert db.source_health()[0]["requests_this_hour"] == 3


def test_config_local_overrides_per_machine(tmp_path):
    from config import load_config

    (tmp_path / "config.yaml").write_text("alerts:\n  desktop: true\n  min_backing_to_alert: 2.0\n")
    (tmp_path / "config.local.yaml").write_text("alerts:\n  desktop: false\n")
    c = load_config(tmp_path)
    assert c["alerts"]["desktop"] is False and c["alerts"]["min_backing_to_alert"] == 2.0
