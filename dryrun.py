"""--dry-run: the whole pipeline on sample data from sample_data/*.json.

Every HTTP call is answered by SampleTransport and X by FakeX, so it runs with
no keys and no network. It walks through a scripted story:
  1. DexScreener feed, X watch/sweep, Telegram messages -> alerts
  2. "15 minutes later": tweet-persistence re-checks (one official tweet stays up
     -> VERIFIED, one signal account deletes its tweet)
  3. "later": you said you're in GoodCat, its liquidity is pulled -> EXIT WARNING, paper trade closes
  4. backtest + qualify on SYNTHETIC history (demonstrates the reports only)
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
import time
from pathlib import Path

import httpx
import yaml

from config import Secrets, deep_merge
from db import DB
from sources.x_source import XTweet, XUser

SAMPLE_DIR = Path(__file__).resolve().parent / "sample_data"


def _load(name: str, sample_dir: Path = SAMPLE_DIR):
    data = json.loads((sample_dir / name).read_text())
    if isinstance(data, dict):
        data.pop("_comment", None)
    return data


def _materialise_pair(p: dict) -> dict:
    p = dict(p)
    ago = p.pop("pairCreatedMinutesAgo", None)
    if ago is not None:
        p["pairCreatedAt"] = int((time.time() - ago * 60) * 1000)
    return p


def _seed(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16)


class SampleTransport(httpx.AsyncBaseTransport):
    def __init__(self, sample_dir: Path = SAMPLE_DIR):
        self.profiles = _load("dex_profiles.json", sample_dir)
        self.pairs = {k.lower(): [_materialise_pair(p) for p in v]
                      for k, v in _load("dex_pairs.json", sample_dir).items()}
        self.rugcheck = _load("rugcheck.json", sample_dir)
        self.goplus = _load("goplus.json", sample_dir)
        self.helius = self._build_helius(_load("helius.json", sample_dir))
        self.websites = _load("websites.json", sample_dir)
        self.pumpfun = _load("pumpfun.json", sample_dir)
        self.calls: list[str] = []

    # -- scenario hooks --------------------------------------------------------
    def set_liquidity(self, token: str, usd: float) -> None:
        for p in self.pairs.get(token.lower(), []):
            p["liquidity"] = {"usd": usd}

    def _build_helius(self, spec: dict) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        now = time.time()
        for pool, s in (spec.get("pools") or {}).items():
            txs = []
            span = s["span_minutes"] * 60 - 60
            for i in range(s["n"]):
                ts = now - span * i / max(1, s["n"] - 1)
                nt = []
                if s.get("tip_every") and i % s["tip_every"] == 0:
                    nt.append({"fromUserAccount": f"trader{i}", "toUserAccount": "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
                               "amount": s["tip"]})
                txs.append({"signature": f"{pool[:6]}sig{i}", "timestamp": int(ts), "fee": s["fee"], "type": "SWAP",
                            "nativeTransfers": nt, "tokenTransfers": []})
            out[pool] = txs
        for wallet, swaps in (spec.get("wallets") or {}).items():
            out[wallet] = [{"signature": f"{wallet[:6]}w{i}", "timestamp": int(now - sw["minutes_ago"] * 60), "fee": 5000,
                            "type": "SWAP", "nativeTransfers": [],
                            "tokenTransfers": [{"mint": sw["mint"], "tokenAmount": sw["amount"],
                                                "toUserAccount": wallet if sw["side"] == "buy" else "pool",
                                                "fromUserAccount": "pool" if sw["side"] == "buy" else wallet}]}
                           for i, sw in enumerate(swaps)]
        return out

    def _candles(self, pool: str, unit: str, agg: int, limit: int) -> list:
        rnd = random.Random(_seed(pool + unit + str(agg)))
        step = (60 if unit == "minute" else 3600) * agg
        now = int(time.time() // step * step)
        price = 0.001
        drift = 0.004 if pool.lower() in {p["pairAddress"].lower() for ps in self.pairs.values() for p in ps
                                          if (p.get("priceChange") or {}).get("h1", 0) > 0} else -0.004
        rows = []
        for i in range(limit):
            o = price
            c = max(1e-9, o * math.exp(drift + rnd.gauss(0, 0.03)))
            h, l = max(o, c) * (1 + abs(rnd.gauss(0, 0.01))), min(o, c) * (1 - abs(rnd.gauss(0, 0.01)))
            rows.append([now - (limit - 1 - i) * step, o, h, l, c, rnd.uniform(1000, 5000) * (1 + i / limit)])
            price = c
        return rows[::-1]  # GeckoTerminal returns newest first

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        host, path = url.host, url.path
        self.calls.append(f"{request.method} {host}{path}")

        if host == "api.dexscreener.com":
            if path == "/token-profiles/latest/v1":
                return httpx.Response(200, json=self.profiles)
            if m := re.fullmatch(r"/latest/dex/tokens/([^/]+)", path):
                return httpx.Response(200, json={"schemaVersion": "1.0.0",
                                                 "pairs": self.pairs.get(m.group(1).lower()) or None})
            if m := re.fullmatch(r"/latest/dex/pairs/([^/]+)/([^/]+)", path):
                pair_addr = m.group(2).lower()
                hits = [p for ps in self.pairs.values() for p in ps if (p.get("pairAddress") or "").lower() == pair_addr]
                return httpx.Response(200, json={"pairs": hits or None})
        if host == "api.rugcheck.xyz":
            if m := re.fullmatch(r"/v1/tokens/([^/]+)/report", path):
                data = self.rugcheck.get(m.group(1))
                return httpx.Response(200, json=data) if data else httpx.Response(404, json={"error": "not found"})
        if host == "api.gopluslabs.io":
            addr = (url.params.get("contract_addresses") or "").lower()
            data = self.goplus.get(addr)
            return httpx.Response(200, json={"code": 1, "message": "OK", "result": {addr: data} if data else {}})
        if host == "api.helius.xyz":
            if m := re.fullmatch(r"/v0/addresses/([^/]+)/transactions", path):
                txs = self.helius.get(m.group(1), [])
                before = url.params.get("before")
                if before:
                    idx = next((i for i, t in enumerate(txs) if t["signature"] == before), len(txs))
                    txs = txs[idx + 1:]
                return httpx.Response(200, json=txs[: int(url.params.get("limit", 100))])
        if host == "api.geckoterminal.com":
            if m := re.fullmatch(r"/api/v2/networks/[^/]+/pools/([^/]+)/ohlcv/(minute|hour)", path):
                rows = self._candles(m.group(1), m.group(2), int(url.params.get("aggregate", 1)),
                                     min(int(url.params.get("limit", 100)), 300))
                return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": rows}}})
        if host == "frontend-api-v3.pump.fun":
            if m := re.fullmatch(r"/coins/user-created-coins/([^/]+)", path):
                return httpx.Response(200, json=self.pumpfun["created"].get(m.group(1), []))
            if m := re.fullmatch(r"/coins/([^/]+)", path):
                c = self.pumpfun["coins"].get(m.group(1))
                return httpx.Response(200, json=c) if c else httpx.Response(404, json={})
        if host in ("eth.blockscout.com", "base.blockscout.com", "api.etherscan.io"):
            return httpx.Response(200, json={"status": "1", "result": []})
        page = self.websites.get(str(url.copy_with(query=None)).rstrip("/") + ("/" if path in ("", "/") else ""))
        if page is None:
            page = self.websites.get(str(url.copy_with(query=None)))
        if page is not None:
            return httpx.Response(200, text=page, headers={"content-type": "text/html"})
        if host in ("discord.com", "api.telegram.org"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"error": f"no sample for {host}{path}"})


class FakeX:
    """Implements the XClient protocol from sample_data/x.json."""

    def __init__(self, sample_dir: Path = SAMPLE_DIR):
        self.data = _load("x.json", sample_dir)
        self.now = time.time()
        self.deleted = set(self.data.get("deleted") or [])
        self.hidden_deleted = True  # deletions only "happen" after the scenario's time skip

    def user(self, uid: str) -> XUser:
        u = self.data["users"][uid]
        return XUser(id=uid, handle=u["handle"], name=u["name"], bio=u.get("bio", ""),
                     pfp=f"https://pbs.twimg.com/p/{uid}.jpg", verified_type=u.get("verified_type"),
                     followers=u["followers"], statuses=u["statuses"],
                     created_at=self.now - u.get("age_days", 365) * 86400, website=u.get("website"),
                     bio_links=[u["website"]] if u.get("website") else [], pinned_ids=u.get("pinned", []))

    def _tweet(self, t: dict, uid: str) -> XTweet:
        kind = t.get("kind", "post")
        return XTweet(id=t["id"], user=self.user(uid), text=t["text"], created_at=self.now - t["minutes_ago"] * 60,
                      kind=kind, reply_to_user_id=t.get("reply_to"))

    async def search(self, query: str, limit: int) -> list[XTweet]:
        return [self._tweet(t, t["user"]) for t in self.data["search"]][:limit]

    async def user_tweets(self, user_id: str, limit: int) -> list[XTweet]:
        return [self._tweet(t, user_id) for t in self.data["timelines"].get(user_id, [])][:limit]

    async def user_by_id(self, user_id: str) -> XUser | None:
        return self.user(user_id) if user_id in self.data["users"] else None

    async def user_by_handle(self, handle: str) -> XUser | None:
        uid = next((k for k, u in self.data["users"].items() if u["handle"].lower() == handle.lower()), None)
        return self.user(uid) if uid else None

    async def tweet(self, tweet_id: str) -> XTweet | None:
        if tweet_id in self.deleted and not self.hidden_deleted:
            return None
        if tweet_id in self.data.get("pinned", {}):
            p = self.data["pinned"][tweet_id]
            return self._tweet({**p, "id": tweet_id}, p["user"])
        for uid, tl in self.data["timelines"].items():
            for t in tl:
                if t["id"] == tweet_id:
                    return self._tweet(t, uid)
        return None

    async def following(self, user_id: str, limit: int) -> list[XUser]:
        return [self.user(u) for u in self.data["following"].get(user_id, [])][:limit]

    async def followers(self, user_id: str, limit: int) -> list[XUser]:
        spec = self.data["followers_sample"].get(user_id)
        if not spec:
            return []
        out = []
        for i in range(min(limit, spec["n"])):
            out.append(XUser(id=f"f{user_id}{i}", handle=f"fan{i}",
                             pfp="" if i < spec["no_pfp"] else "https://pbs.twimg.com/p/x.jpg",
                             statuses=0 if i < spec["zero_tweets"] else 50,
                             created_at=self.now - (10 if i < spec["new"] else 400) * 86400))
        return out

    async def status(self) -> dict:
        return {"accounts": [{"username": "dry-run", "active": True, "logged_in": True, "requests": 0, "error": ""}]}


def sample_messages(sample_dir: Path = SAMPLE_DIR) -> list[dict]:
    return _load("messages.json", sample_dir)


def dry_config(cfg: dict, sample_dir: Path = SAMPLE_DIR) -> dict:
    extra = yaml.safe_load((sample_dir / "dry_config.yaml").read_text()) or {}
    cfg = deep_merge(cfg, {k: v for k, v in extra.items()})
    cfg["rate_limits"] = {host: 100_000 for host in list(cfg["rate_limits"]) + ["corp.example", "goodcat.example"]}
    cfg["x"] = {**cfg["x"], "pace": 0}
    return cfg


def seed_snapshots(db: DB, fx: FakeX) -> None:
    for s in fx.data.get("snapshot_history") or []:
        db.x("""INSERT INTO account_snapshots (user_id, taken_at, handle, name, bio, pfp, verified_type, statuses, followers)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
             (s["user_id"], time.time() - s["days_ago"] * 86400, s["handle"], s["name"], s["bio"], s["pfp"],
              s["verified_type"], s["statuses"], s["followers"]))


def synthetic_history(db: DB, n: int = 180, seed: int = 7) -> None:
    """SYNTHETIC coins + outcomes + paper trades so backtest/qualify have something to show."""
    rnd = random.Random(seed)
    now = time.time()
    for i in range(n):
        first = now - (n - i) * 3 * 3600
        backing = round(rnd.choice([0, 0, 0.2, 1.0, 1.2, 1.5, 2.5, 3.0, 4.5]), 1)
        fees = round(rnd.lognormvariate(0.5, 1.0), 2)
        verdict = rnd.choices(["UNCONFIRMED", "VERIFIED", "DANGER"], [0.75, 0.05, 0.2])[0]
        edge = 0.02 * backing + (0.05 if fees > 1.5 else -0.05)
        path, p = [], 1.0
        for k in range(96):
            p *= math.exp(rnd.gauss(edge / 30 - 0.002, 0.06))
            path.append([first + (k + 1) * 900, p * 1.02, p * 0.98, p])
        rugged = verdict == "DANGER" and rnd.random() < 0.6
        if rugged:
            for k in range(40, 96):
                path[k] = [path[k][0], 0.1, 0.05, 0.08]
        feats = {"backing": backing, "total": backing, "global_fees_sol": fees, "fees_to_volume": rnd.uniform(0, 0.004),
                 "liquidity_usd": rnd.uniform(5e3, 3e5), "top10_pct": rnd.uniform(5, 40), "chart_quality": rnd.random(),
                 "text_sentiment": rnd.uniform(-1, 1), "connection": rnd.choice([0, 0, 0.5, 1.5]),
                 "smart_wallet_buys": rnd.choice([0, 0, 1, 2]), "hour_utc": time.gmtime(first).tm_hour,
                 "safety_lp_locked": rnd.choice([0, 0, 1]) + (1 if verdict == "DANGER" else 0)}
        addr = f"SYNTH{i:04d}"
        db.x("""INSERT INTO feature_snapshots (chain, address, taken_at, first_seen_at, entry_price, verdict, features_json)
                VALUES ('solana', ?, ?, ?, 1.0, ?, ?)""", (addr, first, first, verdict, json.dumps(feats)))
        for h, k in (("15m", 1), ("1h", 4), ("6h", 24), ("24h", 96)):
            seg = path[:k]
            db.x("""INSERT INTO outcomes (chain, address, horizon, recorded_at, max_gain, max_drawdown, final_return, rugged,
                    path_json) VALUES ('solana', ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (addr, h, first + k * 900, max(c[1] for c in seg) - 1, min(c[2] for c in seg) - 1, seg[-1][3] - 1,
                  int(rugged and k >= 40), json.dumps(path) if h == "24h" else None))
        if verdict != "DANGER" and backing >= 1.5:
            pnl = rnd.gauss(-0.02 + 0.01 * backing, 0.35)
            db.x("""INSERT INTO paper_trades (strategy, chain, address, opened_at, entry_price, stake, status, closed_at,
                    exit_price, exit_reason, pnl_pct) VALUES ('backed', 'solana', ?, ?, 1.0, 50, 'closed', ?, ?, 'synthetic', ?)""",
                 (addr, first, first + 6 * 3600, 1 + pnl, max(-100.0, pnl * 100)))


async def run_dry(cfg: dict) -> int:
    from backtest import engine
    from monitor import Monitor
    from net import Http
    from papertrade import qualify
    from pipeline import Pipeline
    from sources.x_source import XSource

    cfg = dry_config(cfg)
    db = DB(":memory:")
    transport = SampleTransport()
    fx = FakeX()
    http = Http(cfg["rate_limits"], transport=transport, base_backoff=0.01, on_request=db.count_request)
    secrets = Secrets(helius_api_key="dry-run")  # fake: every request is answered by SampleTransport
    pipe = Pipeline(cfg, db, http, secrets, console_only=True, x_client=fx)
    monitor = Monitor(pipe, cfg, XSource(fx, db, cfg))
    seed_snapshots(db, fx)
    hr = lambda title: print(f"\n{'#' * 70}\n# {title}\n{'#' * 70}", flush=True)

    print("DRY RUN - sample data only, nothing is sent anywhere, no real money is ever involved.")
    results = []
    try:
        hr("STEP 1a: DexScreener feed + social-graph cache")
        results += await pipe.poll_dex()
        while await pipe.graph.refresh_one():
            pass
        await pipe.wallets.poll_due(limit=50)
        hr("STEP 1b: X - watched accounts (by user ID) and search sweep")
        for t in await monitor.x_source.watch(pipe.discovery.watched_ids()) + await monitor.x_source.sweep():
            results += await pipe.handle_tweet(t)
        hr("STEP 1c: Telegram channels and other posts")
        for m in sample_messages():
            results += await pipe.process_text(m["text"], m["source"], m["source_ref"], m.get("author"))

        hr("STEP 2: 15 minutes later - re-fetching source tweets")
        fx.hidden_deleted = False
        db.x("UPDATE pending_checks SET due_at = ? WHERE kind IN ('tweet_persistence', 'dump_check')", (time.time() - 1,))
        await monitor.checks_step()

        hr("STEP 3: you tap \"I bought\" on GoodCat; later its liquidity is pulled -> exit warning")
        pipe.holdings.add("solana", "6ce9TvjRyG4XEwjEcm16AXyf2hxrXtCEsth429Uk7MwU")
        transport.set_liquidity("6ce9TvjRyG4XEwjEcm16AXyf2hxrXtCEsth429Uk7MwU", 20000)
        await monitor.follow_up_step()
    finally:
        await http.aclose()

    print("\nSummary of first-pass results:")
    for r in results:
        label = r.verdict.label if r.verdict else "-"
        extra = f"  ({r.reason})" if r.reason and r.status != "alerted" else ""
        print(f"  {r.status:<14} {label:<12} {r.chain or '?':<9} {r.address}{extra}")
    trades = db.q("SELECT strategy, address, status, exit_reason, pnl_pct FROM paper_trades")
    print("\nPaper trades (fake money):")
    for t in trades:
        pnl = f"{t['pnl_pct']:+.1f}%" if t["pnl_pct"] is not None else ""
        print(f"  [{t['strategy']}] {t['address'][:12]}… {t['status']} {t['exit_reason'] or ''} {pnl}")
    print(f"\nX leaderboard:\n{pipe.discovery.leaderboard()}")
    print(f"\n{len(transport.calls)} mocked API calls. DB: "
          f"{db.q1('SELECT COUNT(*) AS n FROM sightings')['n']} sightings, "
          f"{db.q1('SELECT COUNT(*) AS n FROM alerts')['n']} alerts, "
          f"{db.q1('SELECT COUNT(*) AS n FROM feature_snapshots')['n']} backtest snapshots.")
    db.close()

    hr("STEP 4: backtest + qualify on SYNTHETIC history (format demo only - not real results)")
    sdb = DB(":memory:")
    synthetic_history(sdb)
    rep = engine.run(sdb, cfg)
    print(engine.format_report(rep))
    print()
    print(qualify(sdb, cfg, rep)[1])
    sdb.close()
    return 0
