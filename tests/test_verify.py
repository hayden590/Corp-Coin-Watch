import time

from db import DB, Sighting
from discovery import Discovery
from safety import FAIL, PASS, UNKNOWN, WARN
from sources.x_source import XUser
from tests.helpers import cfg, http_with, run
from verify import LegitReport, Verifier, ca_in_text, is_public_http_url, matches_narrative, narrative_terms

import httpx

SOL = "6ce9TvjRyG4XEwjEcm16AXyf2hxrXtCEsth429Uk7MwU"


def setup(signals=None, accounts=None, handler=None):
    c = cfg()
    c["signals"] = signals or []
    c["accounts"] = accounts or []
    db = DB()
    disc = Discovery(db, c)
    v = Verifier(db, http_with(handler or (lambda r: httpx.Response(404))), c, disc)
    return db, disc, v


def user(uid, handle, name="N", bio="", vt=None, website=None):
    return XUser(uid, handle, name, bio, "https://p/x.jpg", vt, 1000, 100, time.time() - 400 * 86400, website)


def test_id_based_matching_ignores_handle_and_display_name():
    db, disc, _ = setup(signals=[{"handle": "realtrader", "x_user_id": "42", "tier": 2}])
    # Impostor copies the handle-ish name and display name but has a different ID
    disc.observe_user(user("999", "realtrader_", name="realtrader"))
    assert disc.tier("999").tier == 0
    # The real account renames its handle: still tier 2 because we match on ID
    disc.observe_user(user("42", "renamed_handle"))
    assert disc.tier("42").tier == 2


def test_tier1_never_auto_promoted():
    db, disc, _ = setup()
    db.x("INSERT INTO x_accounts (user_id, status, updated_at) VALUES ('7', 'promoted', 0)")
    assert disc.tier("7").tier == 2


def test_renamed_account_fails_integrity():
    db, disc, v = setup(signals=[{"handle": "vc", "x_user_id": "5", "tier": 2}])
    db.x("""INSERT INTO account_snapshots (user_id, taken_at, handle, name, bio, pfp, verified_type)
            VALUES ('5', ?, 'vc', 'VC', 'bio', 'https://p/x.jpg', NULL)""", (time.time() - 5 * 86400,))
    disc.observe_user(user("5", "vc_official", name="VC | $SCAM", bio="bio"))
    db.add_sighting(Sighting(SOL, "x", "t1", author_id="5"))
    rep = LegitReport()
    v.integrity(SOL, rep)
    c = rep.get("account_integrity")
    assert c.status == FAIL and "handle" in c.detail and "name" in c.detail


def test_old_changes_outside_window_pass():
    db, disc, v = setup(signals=[{"handle": "vc", "x_user_id": "5", "tier": 2}])
    for days, name in ((10, "Old"), (5, "New")):
        db.x("""INSERT INTO account_snapshots (user_id, taken_at, handle, name, bio, pfp, verified_type)
                VALUES ('5', ?, 'vc', ?, '', '', NULL)""", (time.time() - days * 86400, name))
    db.add_sighting(Sighting(SOL, "x", "t1", author_id="5"))
    rep = LegitReport()
    v.integrity(SOL, rep)
    assert rep.get("account_integrity").status == PASS


def test_tweet_persistence_states():
    db, disc, v = setup()
    v.schedule_persistence("t1", SOL, "solana", "5")
    rep = LegitReport()
    v.persistence(SOL, rep)
    assert rep.get("tweet_persists").status == UNKNOWN
    row = db.q1("SELECT id FROM pending_checks")
    db.complete_check(row["id"], "deleted")
    rep = LegitReport()
    v.persistence(SOL, rep)
    assert rep.get("tweet_persists").status == FAIL and "hacked" in rep.get("tweet_persists").detail
    assert rep.persisted is False


def test_official_site_confirms_only_on_org_domain():
    pages = {"https://corp.example/token": f"<p>CA {SOL}</p>"}
    handler = lambda r: httpx.Response(200, text=pages[str(r.url)]) if str(r.url) in pages else httpx.Response(404)
    db, disc, v = setup(accounts=[{"handle": "corp", "x_user_id": "9", "domains": ["corp.example"]}], handler=handler)
    db.add_sighting(Sighting(SOL, "x", "t1", author_id="9"))
    rep = LegitReport()
    run(v.website(SOL, [], rep))
    assert rep.official_confirmed and rep.get("official_website").status == PASS


def test_project_site_is_not_official():
    handler = lambda r: httpx.Response(200, text=f"CA {SOL}")
    db, disc, v = setup(handler=handler)
    rep = LegitReport()
    run(v.website(SOL, ["https://coin.example"], rep))
    assert rep.get("official_website").status == PASS and not rep.official_confirmed


def test_ca_missing_on_site_warns():
    db, disc, v = setup(handler=lambda r: httpx.Response(200, text="nothing"))
    rep = LegitReport()
    run(v.website(SOL, ["https://coin.example"], rep))
    assert rep.get("official_website").status == WARN


def test_business_verified_is_org_with_profile_domain():
    db, disc, _ = setup()
    disc.observe_user(user("11", "brand", vt="business", website="https://www.brand.example/about"))
    assert disc.org_domains("11") == ["brand.example"]


def test_first_time_poster():
    db, disc, v = setup()
    db.add_sighting(Sighting(SOL, "x", "t1", author="newbie", author_id="1"))
    rep = LegitReport()
    v.first_time_poster(SOL, rep)
    assert rep.get("first_time_poster").status == WARN
    db2, disc2, v2 = setup()
    db2.add_sighting(Sighting("0x" + "a" * 40, "x", "t0", author_id="1", seen_at=time.time() - 100))
    db2.add_sighting(Sighting(SOL, "x", "t1", author_id="1"))
    rep2 = LegitReport()
    v2.first_time_poster(SOL, rep2)
    assert rep2.get("first_time_poster").status == PASS


def test_narrative_coin_flag():
    db, disc, v = setup(signals=[{"handle": "mega", "x_user_id": "1", "tier": 1}])
    db.x("INSERT INTO tweets (tweet_id, user_id, text, created_at) VALUES ('t', '1', 'frog season is coming', ?)",
         (time.time() - 3600,))
    rep = LegitReport()
    v.narrative(SOL, "Frog Season", "FSEASON", rep)
    assert rep.get("narrative").status == WARN and "HIGH RISK" in rep.get("narrative").detail
    # ...but if the tier-1 account engaged with the CA directly, it's fine
    db.x("""INSERT INTO endorsements (user_id, handle, tier, weight, address, kind, tweet_id, at)
            VALUES ('1', 'mega', 1, 1, ?, 'post', 'x', 0)""", (SOL,))
    rep = LegitReport()
    v.narrative(SOL, "Frog Season", "FSEASON", rep)
    assert rep.get("narrative").status == PASS


def test_narrative_terms_skip_generic_words():
    assert narrative_terms("The Coin", "INU") == []
    assert not matches_narrative("the frogger game", ["frog"])
    assert matches_narrative("FROG!!", ["frog"])


def test_ssrf_guard():
    assert is_public_http_url("https://coin.example/x")
    for bad in ("http://localhost/", "http://127.0.0.1/", "http://192.168.1.1/", "file:///etc/passwd",
                "http://10.0.0.5/", "http://router.local/"):
        assert not is_public_http_url(bad)


def test_ca_in_text_exact():
    assert ca_in_text(SOL, f"ca:{SOL}.")
    assert not ca_in_text(SOL, SOL + "x")
    assert ca_in_text("0xabc" + "0" * 37, ("0xABC" + "0" * 37))
