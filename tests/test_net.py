import httpx

from tests.helpers import http_with, run


def test_retries_429_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": 1})
    r = run(http_with(handler).get_json("https://api.dexscreener.com/x"))
    assert r.ok and r.data == {"ok": 1} and len(calls) == 3


def test_gives_up_without_raising():
    r = run(http_with(lambda req: httpx.Response(502)).get_json("https://api.dexscreener.com/x"))
    assert not r.ok and r.error == "HTTP 502"


def test_network_error_never_raises():
    def handler(req):
        raise httpx.ConnectError("down")
    r = run(http_with(handler).get_json("https://api.dexscreener.com/x"))
    assert not r.ok and r.error == "ConnectError"


def test_4xx_not_retried_and_bad_json():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(404)
    assert run(http_with(handler).get_json("https://a.io/x")).status == 404 and len(calls) == 1
    r = run(http_with(lambda req: httpx.Response(200, text="<html>")).get_json("https://a.io/x"))
    assert not r.ok and r.error == "invalid JSON"
