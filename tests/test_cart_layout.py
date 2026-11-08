"""Current (2026) Amazon cart layout: div.sc-list-item rows with data-* attributes."""

import pytest
from conftest import FakeResponse, FakeSession, session_getter

from amazon_lyr.exceptions import AmazonError
from amazon_lyr.scraping.api_client import AmazonClient
from amazon_lyr.scraping.parsers import parse_cart

ID1 = "3fb98824-bc03-4efe-a796-1b08c2e7d318"
ID2 = "514c0f63-ee4f-4acc-880c-a9d0a6c3039b"
ID3 = "c027c43d-2b25-4b2d-b60e-5bfc6e59b794"


def _row(kind, item_id, asin, title, price, qty="1", selected="0"):
    qty_box = '<input name="quantityBox" value="1">' if kind == "active" else ""
    price_html = (
        f'<span class="a-price"><span class="a-offscreen">{price}</span></span>' if price else ""
    )
    return f"""
    <div class="a-row sc-list-item" data-asin="{asin}" data-itemid="{item_id}"
         data-itemtype="{kind}" data-quantity="{qty}" data-isselected="{selected}">
      <div class="sc-list-item-content"><div class="sc-list-item-content">
        <span class="a-truncate sc-grid-item-product-title">
          <span class="a-truncate-full">{title}</span></span>
        <div class="sc-item-price-block">{price_html}</div>
        {qty_box}
        <input type="submit" name="submit.delete-{kind}.{item_id}" value="Delete">
      </div></div>
    </div>"""


CART_2026 = f"""
<div id="sc-active-cart" data-name="Active Cart"><div data-name="Active Items">
  {_row("active", ID1, "B000000001", "Glass Kettle 1.7L", "$39.95", selected="1")}
  {_row("active", ID2, "B000000002", "Tea Infuser", "$12.49", qty="2", selected="0")}
</div>
<div id="sc-subtotal-amount-buybox"><span class="sc-price">$39.95</span></div></div>
<div data-name="Saved Cart">
  {_row("saved", ID3, "B000000003", "Gooseneck Kettle", None)}
</div>
"""


def test_parse_cart_2026_layout_splits_active_and_saved():
    cart = parse_cart(CART_2026)
    assert cart["item_count"] == 2 and not cart["empty"]
    first, second = cart["items"]
    assert first == {
        "key": f"submit.delete-active.{ID1}",
        "asin": "B000000001",
        "name": "Glass Kettle 1.7L",
        "price": "$39.95",
        "price_value": 39.95,
        "qty_field": None,
        "quantity": 1,
        "selected": True,
    }
    assert second["quantity"] == 2 and second["selected"] is False
    assert cart["subtotal_value"] == 39.95 and "subtotal_note" in cart
    (saved,) = cart["saved_for_later"]
    assert saved["key"] == f"submit.delete-saved.{ID3}" and saved["price"] is None


def _client(monkeypatch, responses):
    monkeypatch.setattr("amazon_lyr.scraping.api_client.REQUEST_INTERVAL", 0)
    client = AmazonClient(cookies={"at-acbca": "t", "session-id": "s"}, marketplace="amazon.ca")
    fake = FakeSession(responses)
    monkeypatch.setattr(client, "_get_session", session_getter(fake))
    return client, fake


@pytest.mark.parametrize("kind,item_id", [("active", ID1), ("saved", ID3)])
async def test_remove_accepts_2026_delete_keys(monkeypatch, kind, item_id):
    after = CART_2026.replace(f"submit.delete-{kind}.{item_id}", "gone")
    client, fake = _client(monkeypatch, [FakeResponse(after, url="https://www.amazon.ca/gp/cart")])
    result = await client.remove_from_cart(f"submit.delete-{kind}.{item_id}")
    assert result["status"] == "ok"
    assert fake.calls[0]["data"] == {f"submit.delete-{kind}.{item_id}": "Delete"}


async def test_update_qty_explains_js_layout(monkeypatch):
    client, fake = _client(monkeypatch, [])
    with pytest.raises(AmazonError, match="add_to_cart with the new quantity"):
        await client.update_cart_quantity("quantityBox", 2)
    assert fake.calls == []
