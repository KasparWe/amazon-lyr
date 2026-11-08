"""Server assembly + CLI tests."""

import asyncio
import json

from amazon_lyr.cli_main import main as cli_main
from amazon_lyr.server import create_mcp_server

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


def test_server_registers_all_tools(money_ops_enabled):
    mcp = create_mcp_server()
    tools = {t.name for t in asyncio.run(mcp.list_tools())}
    missing = EXPECTED_TOOLS - tools
    assert not missing, f"Missing tools: {missing}"


def test_cli_home_view(capsys):
    cli_main([])
    out = capsys.readouterr().out
    assert "amazon-lyr" in out
    assert "next steps" in out


def test_cli_version(capsys):
    cli_main(["--version"])
    assert capsys.readouterr().out.strip()


def test_cli_status_json(monkeypatch, capsys):
    monkeypatch.setenv("AMAZON_COOKIES", '{"at-main": "t", "session-id": "1"}')
    cli_main(["status", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["authenticated"] is True
    assert data["mode"] == "full"
