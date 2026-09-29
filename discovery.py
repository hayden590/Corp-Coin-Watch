"""Auto-discovery: every X account that posts a CA gets a scorecard.

Tiers:
  1 = mega public figures - ONLY from signals.yaml (never auto-promoted)
  2 = top traders - from signals.yaml, force_include, or auto-promotion
Blacklisted accounts (auto or force_block) give their CAs a scoring penalty.
Accounts are always keyed by numeric X user ID.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from db import DB
from sources.x_source import XUser

log = logging.getLogger(__name__)

SNAPSHOT_FIELDS = ("handle", "name", "bio", "pfp", "verified_type")


@dataclass
class TierInfo:
    tier: int  # 0 = none
    weight: float
    status: str  # normal | promoted | blacklisted
    handle: str | None = None

    @property
    def is_signal(self) -> bool:
        return self.tier in (1, 2) and self.status != "blacklisted"


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    return host.removeprefix("www.") or None


class Discovery:
    def __init__(self, db: DB, cfg: dict):
        self.db = db
        self.cfg = cfg.get("discovery") or {}
        self.snapshot_hours = float((cfg.get("x") or {}).get("snapshot_hours", 24))
        self.signals = {str(s["x_user_id"]): s for s in cfg.get("signals") or [] if s.get("x_user_id")}
        self.orgs = {str(a["x_user_id"]): a for a in cfg.get("accounts") or [] if a.get("x_user_id")}
        self.force_include = {str(u) for u in cfg.get("signals_force_include") or []}
        self.force_block = {str(u) for u in cfg.get("signals_force_block") or []}

    # --- observing accounts -------------------------------------------------
    def observe_user(self, u: XUser) -> None:
        now = time.time()
        self.db.x(
            """INSERT INTO x_accounts (user_id, handle, name, followers, statuses, verified_type, website,
                                       created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET handle=excluded.handle, name=excluded.name,
                 followers=excluded.followers, statuses=excluded.statuses, verified_type=excluded.verified_type,
                 website=excluded.website, created_at=COALESCE(excluded.created_at, x_accounts.created_at),
                 updated_at=excluded.updated_at""",
            (u.id, u.handle, u.name, u.followers, u.statuses, u.verified_type, u.website, u.created_at, now),
        )
        last = self.db.q1("SELECT * FROM account_snapshots WHERE user_id = ? ORDER BY taken_at DESC LIMIT 1", (u.id,))
        changed = last is not None and any((last[f] or "") != (getattr(u, f) or "") for f in SNAPSHOT_FIELDS)
        # Daily snapshot, plus an immediate one whenever something changed.
        if last is None or changed or now - last["taken_at"] >= self.snapshot_hours * 3600:
            self.db.x(
                """INSERT INTO account_snapshots (user_id, taken_at, handle, name, bio, pfp, verified_type,
                                                  statuses, followers) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (u.id, now, u.handle, u.name, u.bio, u.pfp, u.verified_type, u.statuses, u.followers),
            )
            if changed:
                log.warning("X account %s (@%s) changed profile fields", u.id, u.handle)

    # --- tiers --------------------------------------------------------------
    def tier(self, user_id: str | None) -> TierInfo:
        if not user_id:
            return TierInfo(0, 0.0, "normal")
        row = self.db.q1("SELECT status, handle FROM x_accounts WHERE user_id = ?", (user_id,))
        status = row["status"] if row else "normal"
        handle = row["handle"] if row else None
        if user_id in self.force_block:
            return TierInfo(0, 0.0, "blacklisted", handle)
        s = self.signals.get(user_id)
        if s:
            return TierInfo(int(s.get("tier", 2)), float(s.get("weight", 1.0)),
                            "normal" if status != "blacklisted" else status, s.get("handle") or handle)
        if user_id in self.force_include:
            return TierInfo(2, 1.0, "normal", handle)
        if status == "promoted":
            return TierInfo(2, 1.0, "promoted", handle)
        return TierInfo(0, 0.0, status, handle)

    def ids_with_tier(self, tier: int) -> list[str]:
        ids = {uid for uid, s in self.signals.items() if int(s.get("tier", 2)) == tier}
        if tier == 2:
            ids |= self.force_include
            ids |= {r["user_id"] for r in self.db.q("SELECT user_id FROM x_accounts WHERE status = 'promoted'")}
        return sorted(ids - self.force_block)

    def watched_ids(self) -> list[str]:
        return sorted(set(self.ids_with_tier(1)) | set(self.ids_with_tier(2)) | set(self.orgs))

    # --- orgs ---------------------------------------------------------------
    def org_domains(self, user_id: str | None) -> list[str]:
        """Official domains for an org account: accounts.yaml, else gold/business
        verified accounts' profile website."""
        if not user_id:
            return []
        if user_id in self.orgs:
            return [d.lower().removeprefix("www.") for d in self.orgs[user_id].get("domains") or []]
        row = self.db.q1("SELECT verified_type, website FROM x_accounts WHERE user_id = ?", (user_id,))
        if row and row["verified_type"] in ("business", "government"):
            d = domain_of(row["website"])
            return [d] if d else []
        return []

    def is_org(self, user_id: str | None) -> bool:
        return bool(user_id) and (user_id in self.orgs or bool(self.org_domains(user_id)))

    # --- scorecards ---------------------------------------------------------
    def scorecard(self, user_id: str) -> dict:
        acct = self.db.q1("SELECT * FROM x_accounts WHERE user_id = ?", (user_id,))
        calls = self.db.q(
            """SELECT address, MIN(seen_at) AS posted_at FROM sightings
               WHERE author_id = ? AND source = 'x' GROUP BY address""", (user_id,))
        passed = rugged = good = good_base = resolved = safety_known = 0
        earliness = []
        for c in calls:
            addr = c["address"]
            tok = self.db.q1("SELECT chain, last_verdict FROM tokens WHERE address = ? LIMIT 1", (addr,))
            if tok and tok["last_verdict"]:
                safety_known += 1
                passed += tok["last_verdict"] in ("UNCONFIRMED", "VERIFIED")
            outs = {o["horizon"]: o for o in self.db.q("SELECT * FROM outcomes WHERE address = ?", (addr,))}
            if outs:
                resolved += 1
                if any(o["rugged"] for o in outs.values()):
                    rugged += 1
                ref = outs.get("24h") or outs.get("1h")
                if ref:
                    good_base += 1
                    good += (ref["final_return"] or 0) > 0 and not ref["rugged"]
            first = self.db.first_sighting(addr)
            if first:
                earliness.append((c["posted_at"] - first["seen_at"]) / 60)
        pct = lambda n, d: round(100 * n / d, 1) if d else None
        return {
            "user_id": user_id,
            "handle": acct["handle"] if acct else None,
            "followers": acct["followers"] if acct else None,
            "verified_type": acct["verified_type"] if acct else None,
            "status": self.tier(user_id).status,
            "tier": self.tier(user_id).tier,
            "calls": len(calls),
            "resolved": resolved,
            "passed_safety_pct": pct(passed, safety_known),
            "rug_pct": pct(rugged, resolved),
            "good_pct": pct(good, good_base),
            "avg_earliness_min": round(sum(earliness) / len(earliness), 1) if earliness else None,
        }

    def prior_calls(self, user_id: str, before: float, exclude_address: str) -> int:
        return self.db.q1(
            """SELECT COUNT(DISTINCT address) AS n FROM sightings
               WHERE author_id = ? AND source = 'x' AND seen_at < ? AND address != ?""",
            (user_id, before, exclude_address))["n"]

    def apply_rules(self) -> list[str]:
        """Auto-promote / auto-blacklist. Returns human-readable change notes."""
        c = self.cfg
        changes = []
        for row in self.db.q("SELECT DISTINCT author_id FROM sightings WHERE author_id IS NOT NULL AND source = 'x'"):
            uid = row["author_id"]
            if uid in self.signals or uid in self.force_block or uid in self.force_include:
                continue  # manual lists always win
            sc = self.scorecard(uid)
            if sc["resolved"] == 0:
                continue
            current = sc["status"]
            new = current
            if sc["resolved"] >= c.get("blacklist_min_calls", 3) and (sc["rug_pct"] or 0) >= c.get("blacklist_min_rug_pct", 50):
                new = "blacklisted"
            elif (sc["resolved"] >= c.get("promote_min_calls", 5)
                  and (sc["good_pct"] or 0) > c.get("promote_min_good_pct", 40)
                  and (sc["rug_pct"] or 0) < c.get("promote_max_rug_pct", 20)):
                new = "promoted"
            elif current == "promoted":
                new = "normal"  # fell below the bar
            if new != current:
                reason = json.dumps({k: sc[k] for k in ("resolved", "good_pct", "rug_pct")})
                self.db.x("UPDATE x_accounts SET status = ?, status_reason = ? WHERE user_id = ?", (new, reason, uid))
                changes.append(f"@{sc['handle'] or uid}: {current} -> {new} {reason}")
        for ch in changes:
            log.info("discovery: %s", ch)
        return changes

    def leaderboard(self, min_calls: int = 1, n: int = 15) -> str:
        cards = [self.scorecard(r["author_id"]) for r in
                 self.db.q("SELECT DISTINCT author_id FROM sightings WHERE author_id IS NOT NULL AND source = 'x'")]
        cards = [c for c in cards if c["calls"] >= min_calls]
        if not cards:
            return "No X accounts have posted CAs yet."

        def key(c):
            return ((c["good_pct"] or 0) - (c["rug_pct"] or 0), c["resolved"])

        def fmt(c):
            f = lambda v, s="%": "-" if v is None else f"{v}{s}"
            return (f"  @{(c['handle'] or c['user_id'])[:18]:<18} {c['status']:<11} calls {c['calls']:>3} "
                    f"resolved {c['resolved']:>3}  good {f(c['good_pct']):>6}  rug {f(c['rug_pct']):>6}  "
                    f"safe {f(c['passed_safety_pct']):>6}  early {f(c['avg_earliness_min'], 'm'):>7}  "
                    f"followers {c['followers'] or '-'}")

        ranked = sorted(cards, key=key, reverse=True)
        lines = ["BEST accounts (good% - rug%):"] + [fmt(c) for c in ranked[:n]]
        lines += ["", "WORST accounts:"] + [fmt(c) for c in ranked[::-1][:n]]
        lines += ["", "good = price up at 24h (or 1h) vs first sighting; rug = liquidity -80% or honeypot;",
                  "early = minutes after the first sighting from any source (0 = first)."]
        return "\n".join(lines)
