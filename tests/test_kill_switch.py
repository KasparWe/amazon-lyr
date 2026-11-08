"""Kill-switch tests: with the shipped constants, nothing can buy, cancel or return.

These run against the REAL constants (no monkeypatching to True), so if
someone flips a switch in constants.py the first test fails loudly and the
change has to be made on purpose.
"""

import asyncio
import re
from pathlib import Path

import pytest
from conftest import FakeResponse, FakeSession, session_getter
from fastmcp import Client

from amazon_lyr import constants
from amazon_lyr.exceptions import AmazonError, OrderingDisabledError
from amazon_lyr.scraping.api_client import AmazonClient
from amazon_lyr.server import create_mcp_server
from amazon_lyr.session_state import save_cookies

MONEY_TOOLS = {"place_order", "checkout_preview", "cancel_order", "initiate_return"}
FULL = {"at-main": "t", "session-id": "s", "ubid-main": "u"}


def test_shipped_constants_are_off():
    assert constants.ORDERING_ENABLED is False
    assert constants.ORDER_CANCEL_ENABLED is False
    assert constants.RETURNS_ENABLED is False
    # Guard against someone "enabling" via env var or config instead of code.
    src = Path(constants.__file__).read_text()
    assert re.search(r"^ORDERING_ENABLED: Final\[bool\] = False\b", src, re.M)
    assert 'getenv("ORDERING' not in src and "environ" not in src.split("ORDERING_ENABLED")[0]


def test_money_tools_not_registered():
    names = {t.name for t in asyncio.run(create_mcp_server().list_tools())}
    assert not (MONEY_TOOLS & names), f"money tools exposed: {MONEY_TOOLS & names}"
    # Research + list tools still there.
    assert {"search_products", "add_to_wishlist", "get_wishlist", "add_to_cart"} <= names


async def test_agent_cannot_call_place_order_over_protocol(isolated_config):
    save_cookies(FULL)
    async with Client(create_mcp_server()) as client:
        with pytest.raises(Exception, match="(?i)unknown tool|not found"):
            await client.call_tool("place_order", {"confirm": "PLACE_ORDER"})


async def test_instructions_tell_agent_buying_is_disabled():
    async with Client(create_mcp_server()) as client:
        init = client.initialize_result
        assert "disabled" in (init.instructions or "")
        assert "confirm='PLACE_ORDER'" not in (init.instructions or "")


def _client(monkeypatch, responses=None):
    monkeypatch.setattr("amazon_lyr.scraping.api_client.REQUEST_INTERVAL", 0)
    client = AmazonClient(cookies=FULL)
    fake = FakeSession(responses or [FakeResponse("<html></html>")])
    monkeypatch.setattr(client, "_get_session", session_getter(fake))
    return client, fake


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.place_order("PLACE_ORDER"),
        lambda c: c.checkout_preview(),
        lambda c: c._initiate_checkout(),
        lambda c: c.cancel_order("111-2223334-5556667"),
        lambda c: c.initiate_return("111-2223334-5556667"),
    ],
    ids=["place_order", "checkout_preview", "initiate_checkout", "cancel", "return"],
)
async def test_client_methods_refuse_before_any_request(monkeypatch, call):
    client, fake = _client(monkeypatch)
    with pytest.raises(OrderingDisabledError):
        await call(client)
    assert fake.calls == [], "no HTTP request may be sent when the switch is off"


@pytest.mark.parametrize(
    "method,path,data",
    [
        ("POST", "/gp/cart/view.html", {"submit.checkout": "Proceed to checkout"}),
        ("POST", "/gp/buy/spc/handlers/static-submit-decoupled.html", {"x": "1"}),
        ("POST", "/checkout/p/p-123/spc/place-order", {"x": "1"}),
        ("POST", "/some/form", {"placeYourOrder1": "Place your order"}),
        ("POST", "/gp/product/handle-buy-box/ref=dp_start-bbf_1_glance", {"x": "1"}),
        ("POST", "/some/form", {"submit.buy-now": "Buy Now"}),
        ("GET", "/checkout/entry/cart", None),
        ("GET", "/gp/buy/spc/handlers/display.html", None),
        ("POST", "/checkout/turbo-initiate", {"x": "1"}),
    ],
)
async def test_transport_blocks_checkout_requests(monkeypatch, method, path, data):
    client, fake = _client(monkeypatch)
    with pytest.raises(OrderingDisabledError):
        await client._request(method, path, data=data)
    assert fake.calls == []


async def test_transport_allows_normal_shopping(monkeypatch):
    client, fake = _client(monkeypatch)
    await client._request("GET", "/s", params={"k": "checkout counter buy now"})
    await client._request("POST", "/gp/aws/cart/add.html", data={"ASIN.1": "B000000001"})
    assert len(fake.calls) == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/steal",
        "https://amazon.com.evil.example/x",
        "https://evilamazon.com/x",
        "http://www.amazon.com/plaintext",
    ],
)
async def test_transport_refuses_foreign_or_plain_http_hosts(monkeypatch, url):
    client, fake = _client(monkeypatch)
    with pytest.raises(AmazonError, match="non-Amazon or non-https"):
        await client._request("GET", url)
    assert fake.calls == []


@pytest.mark.parametrize("key", ["submit.checkout", "placeYourOrder1", "foo", "submit.delete."])
async def test_remove_from_cart_rejects_non_delete_keys(monkeypatch, key):
    client, fake = _client(monkeypatch)
    with pytest.raises(AmazonError):
        await client.remove_from_cart(key)
    assert fake.calls == []


@pytest.mark.parametrize("field", ["submit.checkout", "placeYourOrder1", "quantity"])
async def test_update_qty_rejects_non_quantity_fields(monkeypatch, field):
    client, fake = _client(monkeypatch)
    with pytest.raises(AmazonError):
        await client.update_cart_quantity(field, 2)
    assert fake.calls == []


async def test_preview_never_clicks_place_order_even_when_enabled(monkeypatch, money_ops_enabled):
    """Single-page checkout: the first page has 'Place your order' but no
    'review your order' text. Preview must stop, not click it."""
    spc = """
    <form method="post" action="/checkout/p/p-1/spc">
      <input type="hidden" name="tok" value="1">
      <input type="submit" name="placeYourOrder1" value="Place your order">
    </form>"""
    cart = """<div class="sc-list-item-content"><input name="submit.delete.C1">
      <span class="sc-product-link">Thing</span></div>"""
    client, fake = _client(monkeypatch, [FakeResponse(cart), FakeResponse(spc), FakeResponse(cart)])
    await client.checkout_preview()
    posted = [c.get("data") or {} for c in fake.calls if c["method"] == "POST"]
    assert not any("placeYourOrder1" in d for d in posted)


def _redirect(location, status=302):
    resp = FakeResponse("", status_code=status)
    resp.headers = {"location": location}
    return resp


@pytest.mark.parametrize(
    "location",
    [
        "/checkout/entry/cart?proceedToCheckout=1",
        "https://www.amazon.com/gp/buy/spc/handlers/display.html",
        "http://www.amazon.com/gp/cart/view.html",
        "https://evil.example/x",
    ],
)
async def test_redirects_are_guarded_hop_by_hop(monkeypatch, location):
    client, fake = _client(monkeypatch, [_redirect(location), FakeResponse("<html></html>")])
    with pytest.raises(AmazonError):
        await client._request("POST", "/gp/cart/view.html", data={"submit.delete.C1": "Delete"})
    assert len(fake.calls) == 1, "the unsafe redirect target must never be requested"


async def test_safe_redirect_is_followed_as_get(monkeypatch):
    client, fake = _client(
        monkeypatch, [_redirect("/gp/cart/view.html?ref=x"), FakeResponse("<html>ok</html>")]
    )
    resp = await client._request("POST", "/gp/aws/cart/add.html", data={"ASIN.1": "B000000001"})
    assert resp.text == "<html>ok</html>"
    assert [c["method"] for c in fake.calls] == ["POST", "GET"]
    assert fake.calls[1]["url"] == "https://www.amazon.com/gp/cart/view.html?ref=x"
    assert "data" not in fake.calls[1]
    assert all(c["allow_redirects"] is False for c in fake.calls)
