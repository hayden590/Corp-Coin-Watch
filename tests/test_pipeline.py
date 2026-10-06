import httpx

import main
from db import DB
from dryrun import SampleTransport
from config import Secrets
from net import Http
from pipeline import Pipeline
from tests.helpers import cfg, run


def make_pipe(transport=None, **overrides):
    db = DB()
    http = Http({}, transport=transport or SampleTransport(), base_backoff=0.001, default_per_minute=100_000)
    return Pipeline(cfg(**overrides), db, http, Secrets(), console_only=True), db


def test_dry_run_end_to_end(capsys):
    from dryrun import run_dry
    assert run(run_dry(cfg())) == 0
    out = capsys.readouterr().out
    assert "DO NOT BUY" in out
    assert "HoneyPot Inu" in out and "SafeFrog" in out
    # DexScreener-feed-only rug is logged, not alerted
    assert "FeedRug" not in out


def test_dex_feed_only_danger_not_alerted_but_social_one_is_when_enabled():
    pipe, db = make_pipe(alerts={"danger_alert_sources": ["x", "telegram", "fomo", "manual"]})
    results = run(pipe.poll_dex())
    feedrug = next(r for r in results if r.address.startswith("755R"))
    assert feedrug.verdict.label == "DANGER" and feedrug.status == "not_alerted"
    # Now someone shills it on Telegram -> DANGER alert goes out
    r = run(pipe.process_text("buy 755RjdyG83PHKNB43kAbCgBRNg4tr4fUaM9KoL7oRKbk", "telegram", "m1"))[0]
    assert r.status == "alerted"


def test_scam_warnings_are_off_by_default():
    pipe, db = make_pipe()
    run(pipe.poll_dex())
    r = run(pipe.process_text("buy 755RjdyG83PHKNB43kAbCgBRNg4tr4fUaM9KoL7oRKbk", "telegram", "m1"))[0]
    assert r.verdict.label == "DANGER" and r.status == "not_alerted"
    assert db.q1("SELECT COUNT(*) AS n FROM feature_snapshots WHERE address LIKE '755R%'")["n"] == 1  # still learned from


def test_pair_link_resolves_to_token_and_records_both():
    pipe, db = make_pipe()
    r = run(pipe.process_text("https://dexscreener.com/ethereum/0x1c754defd932f94bdffd3a9a3f53cb56ac89770f",
                              "telegram", "m2"))[0]
    assert r.address == "0x74fa5327cc0f4e947789dd5e989a61a8242986a5" and r.chain == "ethereum"
    assert db.first_sighting(r.address)["source"] == "telegram"


def test_no_pair_and_no_alert_for_wallets():
    pipe, _ = make_pipe()
    r = run(pipe.process_text("GfsJWjmGXMfct8JMR9Lm9ySUnniZbnGUTQDbT8ipWf9U", "x", "t"))[0]
    assert r.status == "no_pair"


def test_safety_is_cached():
    t = SampleTransport()
    pipe, _ = make_pipe(t)
    text = "6ce9TvjRyG4XEwjEcm16AXyf2hxrXtCEsth429Uk7MwU"
    run(pipe.process_text(text, "x", "a"))
    n = sum("rugcheck" in c for c in t.calls)
    run(pipe.process_text(text, "telegram", "b"))
    assert sum("rugcheck" in c for c in t.calls) == n


def test_source_down_alerts_once(capsys):
    pipe, db = make_pipe(httpx.MockTransport(lambda r: httpx.Response(503)))
    for _ in range(7):
        run(pipe.poll_dex())
    out = capsys.readouterr().out
    assert out.count("is DOWN") == 1
    assert db.source_health()[-1]["consecutive_failures"] >= 5


def test_api_outage_never_crashes():
    t = SampleTransport()

    class Mixed(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if "dexscreener" in request.url.host:
                return await t.handle_async_request(request)
            raise httpx.ConnectError("down")

    pipe, _ = make_pipe(Mixed())
    r = run(pipe.process_text("6ce9TvjRyG4XEwjEcm16AXyf2hxrXtCEsth429Uk7MwU", "x", "t"))[0]
    assert r.verdict.label == "UNCHECKED"


def test_no_trading_code_exists():
    """HARD RULE guard: no signing / swap / private-key code anywhere in the project."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    banned = ("private_key", "sign_transaction", "send_transaction", "sendTransaction", "Keypair", "swap(")
    for py in root.rglob("*.py"):
        if ".venv" in py.parts or py.name == "test_pipeline.py":
            continue
        text = py.read_text()
        for word in banned:
            assert word not in text, f"{word} found in {py}"
