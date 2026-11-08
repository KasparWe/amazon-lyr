"""Protocol-level MCP tests using fastmcp's in-memory Client.

Exercises the full stack: FastMCP wiring → tool functions → dependencies →
client → parsers, plus auth gating as an agent would experience it.
"""

import json

import pytest
from fastmcp import Client

from amazon_lyr import dependencies
from amazon_lyr.scraping.api_client import AmazonClient
from amazon_lyr.server import create_mcp_server
from amazon_lyr.session_state import save_cookies

# Upstream money-flow tests: run with the kill switches flipped on.
pytestmark = pytest.mark.usefixtures("money_ops_enabled")


EXPECTED_TOOLS = {
    "search_products",
    "get_product",
    "compare_products",
    "get_reviews",
    "review_insights",
    "search_in_reviews",
    "add_to_cart",
    "view_cart",
    "remove_from_cart",
    "update_cart_quantity",
    "get_wishlist",
    "get_orders",
    "get_order_details",
    "track_shipment",
    "list_returns",
    "initiate_return",
    "cancel_order",
    "checkout_preview",
    "place_order",
    "add_to_wishlist",
    "get_session_status",
}


def _annotations(tool):
    ann = tool.annotations
    if hasattr(ann, "readOnlyHint"):
        return getattr(ann, "readOnlyHint", None)
    return (ann or {}).get("readOnlyHint")


async def test_list_tools_over_protocol():
    async with Client(create_mcp_server()) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        assert EXPECTED_TOOLS <= names


async def test_read_annotations_survive_registration():
    async with Client(create_mcp_server()) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert _annotations(tools["search_products"]) is True
        assert _annotations(tools["get_product"]) is True


async def test_session_status_tool_works_logged_out(isolated_config):
    async with Client(create_mcp_server()) as client:
        result = await client.call_tool("get_session_status", {})
        assert result.data["mode"] == "none"


async def test_search_without_cookies_returns_login_hint(isolated_config):
    async with Client(create_mcp_server()) as client:
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError) as e:
            await client.call_tool("search_products", {"query": "laptop"})
        assert "amazon-lyr login" in str(e.value)


async def test_cart_write_gated_without_context_cookies(isolated_config):
    # at-main present → reads allowed, but writes still gated.
    save_cookies({"at-main": "t"})
    async with Client(create_mcp_server()) as client:
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError) as e:
            await client.call_tool("add_to_cart", {"asin": "B0TEST", "quantity": 1})
        assert "amazon-lyr login" in str(e.value)


async def test_money_movements_gated_behind_full_session(isolated_config):
    """Returns, cancellations, checkout: must never run without full session
    cookies, even with a valid at-main."""
    save_cookies({"at-main": "t"})
    async with Client(create_mcp_server()) as client:
        from fastmcp.exceptions import ToolError

        for tool, args in (
            ("initiate_return", {"order_id": "111-2223334-5556667"}),
            ("cancel_order", {"order_id": "111-2223334-5556667"}),
            ("checkout_preview", {}),
            ("place_order", {"confirm": "PLACE_ORDER"}),
            ("add_to_wishlist", {"asin": "B0X1234567"}),
        ):
            with pytest.raises(ToolError) as e:
                await client.call_tool(tool, args)
            assert "amazon-lyr login" in str(e.value), f"{tool} was not gated"


async def test_place_order_rejects_wrong_confirm_even_with_full_session(isolated_config):
    """The literal confirm string is the last line of defense against an agent
    placing orders without explicit user approval."""
    save_cookies({"at-main": "t", "session-id": "s", "ubid-main": "u"})
    async with Client(create_mcp_server()) as client:
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError) as e:
            await client.call_tool("place_order", {"confirm": "yes"})
        assert "PLACE_ORDER" in str(e.value)


async def test_destructive_tools_annotated(isolated_config):
    async with Client(create_mcp_server()) as client:
        tools = {t.name: t for t in await client.list_tools()}

        def hint(name, key):
            ann = tools[name].annotations
            val = getattr(ann, key, None)
            return ann.get(key) if val is None and isinstance(ann, dict) else val

        # Cancelling destroys the order state — destructive.
        assert bool(hint("cancel_order", "destructiveHint"))
        # A return *creates* something; it's non-destructive but irreversible-ish.
        assert not hint("initiate_return", "destructiveHint")
        assert not hint("initiate_return", "readOnlyHint")
        assert hint("initiate_return", "idempotentHint") is False


async def test_view_cart_happy_path_through_protocol(isolated_config, monkeypatch):
    save_cookies({"at-main": "t", "session-id": "s", "ubid-main": "u"})
    cart_html = """
    <div class="sc-list-item-content">
      <input type="submit" name="submit.delete.K1" value="Delete">
      <span class="sc-product-link">Cable</span>
      <span class="sc-product-price">$5.00</span>
    </div>"""

    class FakeSession:
        async def request(self, *a, **k):
            return type("R", (), {"text": cart_html, "status_code": 200, "headers": {}})()

        async def close(self):
            pass

    client_obj = AmazonClient(cookies={"at-main": "t"})
    monkeypatch.setattr(client_obj, "_get_session", _fake_get(FakeSession()))
    dependencies._client = client_obj

    try:
        async with Client(create_mcp_server()) as mcp_client:
            result = await mcp_client.call_tool("view_cart", {})
            data = result.data
            assert data["item_count"] == 1
            assert data["items"][0]["name"] == "Cable"
    finally:
        dependencies._client = None


async def test_add_to_cart_clamped_through_protocol(isolated_config, monkeypatch):
    save_cookies({"at-main": "t", "ubid-main": "u"})
    from conftest import buy_box_page, cart_page_with

    captured = {}

    class FakeSession:
        async def request(self, method, url, **kwargs):
            if method == "POST":
                captured.update(kwargs)
            text = buy_box_page("B0CLAMP001") if "/dp/" in url else cart_page_with("B0CLAMP001")
            return type("R", (), {"text": text, "status_code": 200, "headers": {}})()

        async def close(self):
            pass

    monkeypatch.setattr("amazon_lyr.scraping.api_client.REQUEST_INTERVAL", 0)
    client_obj = AmazonClient(cookies={"at-main": "t"})
    monkeypatch.setattr(client_obj, "_get_session", _fake_get(FakeSession()))
    dependencies._client = client_obj

    try:
        async with Client(create_mcp_server()) as mcp_client:
            result = await mcp_client.call_tool(
                "add_to_cart", {"asin": "B0CLAMP001", "quantity": 999}
            )
            assert result.data["requested"]["quantity"] == 30
            assert captured["data"]["items[0.base][quantity]"] == "30"
    finally:
        dependencies._client = None


async def test_compare_products_rejects_over_five():
    async with Client(create_mcp_server()) as client:
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError, match="between 1 and 5"):
            await client.call_tool("compare_products", {"asins": ["A"] * 6})


def _fake_get(session):
    async def _get():
        return session

    return _get


def test_env_json_roundtrip_for_agents(isolated_config):
    """Document the env-var contract agents rely on."""
    import os

    os.environ["AMAZON_COOKIES"] = json.dumps({"at-main": "abc123"})
    try:
        from amazon_lyr.dependencies import get_amazon_client

        client = get_amazon_client()
        assert client._cookies == {"at-main": "abc123"}
    finally:
        del os.environ["AMAZON_COOKIES"]
