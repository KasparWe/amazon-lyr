from fastmcp import FastMCP

from amazon_lyr.authentication import validate_write_session
from amazon_lyr.dependencies import get_amazon_client
from amazon_lyr.error_handler import handle_api_error


def _require_write_session() -> None:
    if not validate_write_session():
        raise RuntimeError(
            "Writes need full session cookies (login token + session-id/ubid). "
            "Run `amazon-lyr login` to re-import cookies from your browser."
        )


def register_cart_tools(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def add_to_cart(asin: str, quantity: int = 1) -> dict:
        """Add a product to the Amazon shopping cart (quantity clamped to 1-30).
        status is "ok" only when the item shows up in the cart afterwards."""
        try:
            _require_write_session()
            client = get_amazon_client()
            return await client.add_to_cart(asin, quantity=quantity)
        except Exception as e:
            handle_api_error(e)

    @mcp.tool(annotations={"readOnlyHint": True})
    async def view_cart() -> dict:
        """View the cart: items (with delete keys, quantity, selected) and
        saved_for_later. Amazon's subtotal only counts selected (ticked) items."""
        try:
            client = get_amazon_client()
            return await client.view_cart()
        except Exception as e:
            handle_api_error(e)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def remove_from_cart(item_key: str) -> dict:
        """Remove a cart or saved-for-later item using its `key` from view_cart
        (items[].key or saved_for_later[].key)."""
        try:
            _require_write_session()
            client = get_amazon_client()
            return await client.remove_from_cart(item_key)
        except Exception as e:
            handle_api_error(e)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def update_cart_quantity(qty_field: str, quantity: int) -> dict:
        """Change a cart item's quantity (clamped 1-30). qty_field comes from
        view_cart items[].qty_field; if it is null, remove the item and add it again
        with the wanted quantity instead."""
        try:
            _require_write_session()
            client = get_amazon_client()
            return await client.update_cart_quantity(qty_field, quantity)
        except Exception as e:
            handle_api_error(e)


def register_wishlist_tools(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_lists() -> dict:
        """All your Amazon lists (wish lists): list_id, name and privacy
        (private/shared/public). Use a list_id with the other list tools."""
        try:
            _require_write_session()
            return await get_amazon_client().get_lists()
        except Exception as e:
            handle_api_error(e)

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_wishlist(list_id: str | None = None) -> dict:
        """Items on one of your lists (list_id from get_lists); your default
        list when list_id is omitted."""
        try:
            _require_write_session()
            return await get_amazon_client().get_wishlist(list_id)
        except Exception as e:
            handle_api_error(e)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
    async def add_to_wishlist(asin: str, list_id: str | None = None) -> dict:
        """Add a product to one of your lists (list_id from get_lists; the first
        list when omitted). status: ok, already_in_list or not_confirmed."""
        try:
            _require_write_session()
            return await get_amazon_client().add_to_list(asin, list_id)
        except Exception as e:
            handle_api_error(e)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def create_list(name: str, privacy: str = "private", asin: str | None = None) -> dict:
        """Create a new list. privacy: private (default), shared or public.
        Optionally pass an asin to add as the first item. Returns the new list_id."""
        try:
            _require_write_session()
            return await get_amazon_client().create_list(name, privacy=privacy, asin=asin)
        except Exception as e:
            handle_api_error(e)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def remove_from_list(list_id: str, asin: str) -> dict:
        """Remove one product (by asin) from one of your lists."""
        try:
            _require_write_session()
            return await get_amazon_client().remove_from_list(list_id, asin)
        except Exception as e:
            handle_api_error(e)


def register_order_tools(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_orders(
        status_filter: str | None = None,
    ) -> dict:
        """List order history with statuses and totals. status_filter examples:
        last30, months-3, months-6, year-2026, year-2025, pending, shipped."""
        try:
            _require_write_session()
            client = get_amazon_client()
            return await client.get_orders(status_filter)
        except Exception as e:
            handle_api_error(e)
