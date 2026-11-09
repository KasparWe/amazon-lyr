"""Checkout workflow, order placement, and wishlist add — flows + safety."""

import pytest
from conftest import (
    ORDER_ID,
    FakeResponse,
    FakeSession,
    session_getter,
)

from amazon_lyr.exceptions import AmazonError
from amazon_lyr.scraping.api_client import AmazonClient
from amazon_lyr.scraping.parsers import parse_checkout_form, parse_order_confirmation

# Upstream money-flow tests: run with the kill switches flipped on.
pytestmark = pytest.mark.usefixtures("money_ops_enabled")


CART_HTML = """
<div class="sc-list-item-content">
  <input type="submit" name="submit.delete.K1" value="Delete">
  <span class="sc-product-link">Test Item</span>
  <span class="sc-product-price">$10.00</span>
</div>
"""

ADDRESS_STEP_HTML = """
<form method="post" action="/gp/checkout/address/shipping-address.html">
  <input type="hidden" name="anti-csrftoken-a2z" value="tokA">
  <input type="radio" name="addressSelection" value="ADDR1" checked>
  <label>Home - 12 Main St</label>
  <input type="radio" name="addressSelection" value="ADDR2">
  <label>Office - 99 Work Ave</label>
  <input type="submit" name="shipToThisAddress" value="Ship here">
</form>
"""

PAYMENT_STEP_HTML = """
<form method="post" action="/gp/checkout/pay/pay-selection.html">
  <input type="hidden" name="anti-csrftoken-a2z" value="tokB">
  <input type="radio" name="payment-selection" value="VISA4242" checked>
  <label>Visa ending 4242</label>
  <input type="submit" name="continuePayment" value="Continue">
</form>
"""

REVIEW_STEP_HTML = """
<p>Review your order before placing.</p>
<form method="post" action="/gp/checkout/place-order/place-order.html">
  <input type="hidden" name="anti-csrftoken-a2z" value="tokC">
  <div data-test-id='order-summary'>
    <span class="grand-total-price">$10.99</span>
  </div>
  <input type="submit" name="placeYourOrder1" value="Place your order">
</form>
"""

CONFIRMATION_HTML = """
<h1>Order placed, thanks!</h1>
<span>Order # 111-2223334-5556667</span>
<span class="grand-total-price">$10.99</span>
<span class="delivery-date">Arriving Thursday</span>
"""

OTP_HTML = (
    "<html><body>Please verify your card. Enter the one-time password sent to you.</body></html>"
)


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    """Sequential multi-request flows sleep REQUEST_INTERVAL between pages."""
    monkeypatch.setattr("amazon_lyr.scraping.api_client.REQUEST_INTERVAL", 0)


def _client(monkeypatch, responses):
    client = AmazonClient(cookies={"at-main": "t", "session-id": "s", "ubid-main": "u"})
    fake = FakeSession(responses)
    monkeypatch.setattr(client, "_get_session", session_getter(fake))
    return client, fake


# ── parser ───────────────────────────────────────────────────────────────


def test_parse_checkout_form_radios_and_tokens():
    step = parse_checkout_form(ADDRESS_STEP_HTML)
    assert "shipping-address" in step["action"]
    assert step["fields"]["anti-csrftoken-a2z"] == "tokA"
    radios = [c for c in step["choices"] if c["kind"] == "radio"]
    assert len(radios) == 2
    pre = [r for r in radios if r["preselected"]]
    assert pre[0]["value"] == "ADDR1"
    assert step["page_markers"]["address"] is True


def test_parse_order_confirmation():
    conf = parse_order_confirmation(CONFIRMATION_HTML)
    assert conf["placed"] is True
    assert conf["order_id"] == ORDER_ID
    assert conf["total"] == "$10.99"


def test_parse_order_confirmation_none_on_other_pages():
    assert parse_order_confirmation(REVIEW_STEP_HTML) is None


# ── place_order safety contract ──────────────────────────────────────────


async def test_place_order_refuses_without_exact_confirm_literal(monkeypatch):
    client, fake = _client(monkeypatch, [FakeResponse(CART_HTML)] * 5)
    for bad in ("yes", "place_order", "", "PLACE ORDER", None):
        with pytest.raises(AmazonError, match="PLACE_ORDER"):
            await client.place_order(bad)
    assert all("checkout" not in str(c["url"]) for c in fake.calls)


async def test_place_order_full_workflow(monkeypatch):
    responses = [
        FakeResponse(CART_HTML),  # view_cart (empty check)
        FakeResponse(ADDRESS_STEP_HTML),  # initiation → address
        FakeResponse(PAYMENT_STEP_HTML),  # address submit → payment
        FakeResponse(REVIEW_STEP_HTML),  # payment submit → review
        FakeResponse(CONFIRMATION_HTML),  # place → confirmation
    ]
    client, fake = _client(monkeypatch, responses)
    result = await client.place_order("PLACE_ORDER")
    assert result["status"] == "placed"
    assert result["order_id"] == ORDER_ID
    # Final POST must carry the review token and hit the place-order action.
    final_post = fake.calls[-1]
    assert "place-order" in final_post["url"]
    assert final_post["data"]["anti-csrftoken-a2z"] == "tokC"


async def test_place_order_address_override_used(monkeypatch):
    responses = [
        FakeResponse(CART_HTML),
        FakeResponse(ADDRESS_STEP_HTML),
        FakeResponse(CONFIRMATION_HTML),
    ]
    client, fake = _client(monkeypatch, responses)
    await client.place_order("PLACE_ORDER", address_id="ADDR2")
    addr_post = next(c for c in fake.calls if "address" in str(c["url"]))
    assert addr_post["data"]["addressSelection"] == "ADDR2"


async def test_place_order_needs_verification_reported(monkeypatch):
    responses = [
        FakeResponse(CART_HTML),
        FakeResponse(ADDRESS_STEP_HTML),
        FakeResponse(OTP_HTML),
        FakeResponse(OTP_HTML),  # stable tail
    ]
    client, _ = _client(monkeypatch, responses)
    result = await client.place_order("PLACE_ORDER")
    assert result["status"] == "needs_verification"
    assert "browser" in result["note"]


async def test_checkout_preview_stops_before_placing(monkeypatch):
    responses = [
        FakeResponse(CART_HTML),
        FakeResponse(ADDRESS_STEP_HTML),
        FakeResponse(PAYMENT_STEP_HTML),
        FakeResponse(REVIEW_STEP_HTML),
        FakeResponse(CONFIRMATION_HTML),
    ]
    client, fake = _client(monkeypatch, responses)
    preview = await client.checkout_preview()
    assert preview["stage"] == "review"
    assert preview["totals"] == "$10.99"
    assert "Preview only" in preview["note"]
    # Nothing was ever submitted to the place-order endpoint.
    assert not any("place-order" in str(c["url"]) for c in fake.calls)


async def test_checkout_empty_cart_rejected(monkeypatch):
    empty = '<div id="sc-empty-cart"></div>'
    client, _ = _client(monkeypatch, [FakeResponse(empty)])
    with pytest.raises(AmazonError, match="Cart is empty"):
        await client.checkout_preview()
