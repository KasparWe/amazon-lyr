"""Marketplace selection: amazon.ca (and others) without weakening the transport guard."""

import pytest
from conftest import FakeResponse, FakeSession, session_getter

from amazon_lyr.authentication import get_auth_status, validate_write_session
from amazon_lyr.cookie_import import cookies_from_json
from amazon_lyr.dependencies import get_amazon_client
from amazon_lyr.exceptions import AmazonError
from amazon_lyr.marketplace import get_marketplace, resolve_marketplace
from amazon_lyr.scraping.api_client import AmazonClient
from amazon_lyr.scraping.parsers import _scan_price_text, _soup, parse_order_details
from amazon_lyr.session_state import save_cookies

CA = {"at-acbca": "t", "session-id": "s", "ubid-acbca": "u"}


@pytest.mark.parametrize(
    "value", ["amazon.ca", "www.amazon.ca", "https://www.amazon.ca", "https://www.amazon.ca/"]
)
def test_get_marketplace_accepts_ca_forms(value):
    m = get_marketplace(value)
    assert (m.base_url, m.domain, m.login_cookie) == (
        "https://www.amazon.ca",
        "amazon.ca",
        "at-acbca",
    )


@pytest.mark.parametrize(
    "value",
    [
        "evil.example",
        "http://www.amazon.ca",
        "https://www.amazon.ca.evil.example",
        "https://www.amazon.ca/some/path",
        "amazon.zz",
    ],
)
def test_get_marketplace_rejects_unknown_hosts(value):
    with pytest.raises(ValueError, match="Unsupported Amazon marketplace"):
        get_marketplace(value)


def test_resolve_infers_from_cookies(monkeypatch):
    monkeypatch.delenv("AMAZON_MARKETPLACE", raising=False)
    assert resolve_marketplace(CA).domain == "amazon.ca"
    assert resolve_marketplace({"at-main": "t"}).domain == "amazon.com"
    assert resolve_marketplace({}).domain == "amazon.com"
    with pytest.raises(ValueError, match="several marketplaces"):
        resolve_marketplace({"at-main": "t", "at-acbca": "t"})


def test_env_override_wins_and_is_validated(monkeypatch):
    monkeypatch.setenv("AMAZON_MARKETPLACE", "amazon.ca")
    assert resolve_marketplace({"at-main": "t"}).domain == "amazon.ca"
    monkeypatch.setenv("AMAZON_MARKETPLACE", "https://attacker.example")
    with pytest.raises(ValueError):
        resolve_marketplace(CA)


def test_ca_session_is_full_write_session(isolated_config):
    save_cookies(CA)
    assert validate_write_session()
    status = get_auth_status()
    assert status["mode"] == "full" and status["marketplace"] == "amazon.ca"


def test_dependencies_build_ca_client(isolated_config):
    save_cookies(CA)
    client = get_amazon_client()
    assert client._base == "https://www.amazon.ca"


async def test_ca_client_guard_scopes_to_amazon_ca(monkeypatch):
    monkeypatch.setattr("amazon_lyr.scraping.api_client.REQUEST_INTERVAL", 0)
    client = AmazonClient(cookies=CA, marketplace="https://www.amazon.ca")
    fake = FakeSession([FakeResponse("<html></html>", url="https://www.amazon.ca/s?k=x")])
    monkeypatch.setattr(client, "_get_session", session_getter(fake))
    await client._request("GET", "/s", params={"k": "kettle"})
    assert fake.calls[0]["url"] == "https://www.amazon.ca/s"
    for url in ("https://www.amazon.com/s", "http://www.amazon.ca/s"):
        with pytest.raises(AmazonError, match="non-Amazon or non-https"):
            await client._request("GET", url)
    assert len(fake.calls) == 1


def test_ca_client_scopes_cookies_to_amazon_ca():
    import asyncio

    client = AmazonClient(cookies=CA, marketplace="amazon.ca")
    session = asyncio.run(client._get_session())
    assert {c.domain for c in session.cookies.jar} == {".amazon.ca"}
    asyncio.run(client.close())


def test_order_details_links_use_marketplace_base():
    html = '<a href="/gp/css/summary/print.html?invoice=1">Invoice</a>'
    out = parse_order_details(html, base="https://www.amazon.ca")
    assert out["invoice_url"] == "https://www.amazon.ca/gp/css/summary/print.html?invoice=1"


@pytest.mark.parametrize("text", ["CDN$ 24.99", "C$24.99", "$24.99", "CAD 24.99"])
def test_scan_price_handles_canadian_formats(text):
    assert _scan_price_text(_soup(f"<div>Kettle {text} delivery</div>")).endswith("24.99")


def test_cookie_editor_list_export_keeps_amazon_cookies_only():
    data = [
        {"name": "at-acbca", "value": "t", "domain": ".amazon.ca"},
        {"name": "session-id", "value": "s", "domain": "www.amazon.ca"},
        {"name": "tracker", "value": "x", "domain": ".doubleclick.net"},
    ]
    assert cookies_from_json(data) == {"at-acbca": "t", "session-id": "s"}
    assert cookies_from_json({"at-main": "t"}) == {"at-main": "t"}
    with pytest.raises(ValueError):
        cookies_from_json("nope")
