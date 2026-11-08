"""Shared fixtures + transport fakes for all amazon-lyr tests."""

import pytest

# Isolate config dir + env so tests never touch real cookies.


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    xdg = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("AMAZON_COOKIES", raising=False)
    monkeypatch.delenv("AMAZON_MARKETPLACE", raising=False)
    return tmp_path


@pytest.fixture
def money_ops_enabled(monkeypatch):
    """Flip the code-constant kill switches ON for tests of the upstream
    checkout/return/cancel flows. Production code has no way to do this."""
    from amazon_lyr import constants

    monkeypatch.setattr(constants, "ORDERING_ENABLED", True)
    monkeypatch.setattr(constants, "ORDER_CANCEL_ENABLED", True)
    monkeypatch.setattr(constants, "RETURNS_ENABLED", True)


@pytest.fixture(autouse=True)
def _reset_client_singleton():
    """Keep the process-wide client singleton from leaking between tests."""
    from amazon_lyr import dependencies

    dependencies._client = None
    yield
    dependencies._client = None


# ── HTTP transport fakes ─────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, text="", status_code=200, url="https://www.amazon.com/s?k=x"):
        self.text = text
        self.status_code = status_code
        self.url = url
        self.headers = {}

    def raise_for_status(self):
        pass


class FakeSession:
    """Records every request; replays a list of responses (or raises them)."""

    def __init__(self, responses):
        self._responses = [responses] if not isinstance(responses, list) else list(responses)
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": str(url), **kwargs})
        item = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        pass


def attach_session(client, session):
    """Point an existing client at a fake session."""

    async def _get():
        return session

    client._get_session = _get
    return client


def session_getter(session):
    """Returns a zero-arg callable yielding the session, for monkeypatching."""

    async def _get():
        return session

    return _get


# ── shared HTML fixtures ─────────────────────────────────────────────────

ORDER_ID = "111-2223334-5556667"

ORDER_DETAILS_HTML = """
<div id="orderDetails">
  <span data-test-id='order-status'>Shipped</span>
  <div class="yo-item-block">
    <a class="yohtmlc-product-title">USB-C Hub 7-in-1</a>
    <span class="yohtmlc-item-price">$34.99</span>
    <span>Qty: 2</span>
  </div>
  <table class="od-subtotals">
    <tr><td>Subtotal:</td><td>$69.98</td></tr>
    <tr><td>Shipping:</td><td>$0.00</td></tr>
    <tr><td>Total:</td><td>$73.47</td></tr>
  </table>
  <div id="payment-method">Visa ending 4242</div>
  <div id="primaryShipToAddr">John D, 12 Main St</div>
  <div class="tracking-number"><a>TBA123456789</a></div>
  <a href="/gp/aw/invoice?orderID=111-2223334-5556667">View invoice</a>
  <a href="/gp/cancel-your-orders?orderID=111-2223334-5556667">Cancel items</a>
  <span>Order # 111-2223334-5556667</span>
</div>
"""

RETURN_FORM_HTML = """
<form method="post" action="/gp/orc/return/step2">
  <input type="hidden" name="anti-csrftoken-a2z" value="tok123">
  <input type="hidden" name="orderID" value="111-2223334-5556667">
  <div><input type="checkbox" name="itemSelector" value="B0AAA">
    <span class="a-label-bold">USB-C Hub</span></div>
  <div><input type="checkbox" name="itemSelector" value="B0BBB">
    <span class="a-label-bold">Cable Pack</span></div>
  <select name="reasonSelectId">
    <option value="NO_LONGER_NEED">No longer needed</option>
    <option value="DEFECTIVE_ITEM">Defective</option>
  </select>
</form>
"""

CANCEL_PAGE_HTML = """
<form method="post" action="/gp/cancel-your-orders/confirm">
  <input type="hidden" name="orderID" value="111-2223334-5556667">
  <input type="checkbox" name="checkedItems" value="line1">
</form>
"""


def buy_box_page(asin: str = "B0TESTASIN") -> str:
    """Product page with a one-time buy box (and a Subscribe & Save one)."""
    return f"""
<form id="addToCart" method="post" action="/gp/product/handle-buy-box/ref=dp_start-bbf_1_glance">
  <input type="hidden" name="anti-csrftoken-a2z" value="csrf-tok">
  <input type="hidden" name="items[0.base][asin]" value="{asin}">
  <input type="hidden" name="clientName" value="OffersX_OfferDisplay_DetailPage">
  <input type="hidden" name="items[0.base][offerListingId]" value="offer-123">
  <input type="hidden" name="isBuyNow" value="0">
  <input type="submit" name="submit.add-to-cart" value="Add to cart">
  <input type="submit" name="submit.buy-now" value="">
</form>
<form id="addToCart" method="post" action="/gp/product/handle-buy-box/ref=dp_start-bbf_1_glance">
  <input type="hidden" name="items[0.base][additionalParameters][recurringDelivery][type]" value="SNS">
  <input type="hidden" name="anti-csrftoken-a2z" value="sns-tok">
  <input type="hidden" name="items[0.base][offerListingId]" value="sns-offer">
  <input type="submit" name="submit.add-to-cart" value="Subscribe">
</form>"""


def cart_page_with(asin: str) -> str:
    return f"""<form method="post" action="/cart/ref=ord_cart_shr?app-nav-type=none">
<input type="hidden" name="anti-csrftoken-a2z" value="cart-tok">
<div class="a-row sc-list-item" data-asin="{asin}" data-itemid="K1" data-itemtype="active"
     data-quantity="2" data-isselected="1">
  <span class="a-truncate-full">Thing</span>
  <input type="submit" name="submit.delete-active.K1" value="Delete">
</div></form>"""
