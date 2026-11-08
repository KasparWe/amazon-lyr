"""Order lifecycle tools: details, tracking, returns/refunds, cancellation.

Mutating tools are deliberately MCP-only (no raw CLI flags): returns and
cancellations move real money and deserve conversational confirmation.
"""

from typing import Literal

from fastmcp import FastMCP

from amazon_lyr import constants
from amazon_lyr.dependencies import get_amazon_client
from amazon_lyr.error_handler import handle_api_error
from amazon_lyr.tools.shopping import _require_write_session

RETURN_REASON = Literal[
    "no-longer-needed",
    "defective",
    "damaged",
    "wrong-item",
    "missing-parts",
    "better-price",
    "not-as-described",
    "arrived-too-late",
    "size-fit-issue",
    "accidental-order",
]


def register_order_mgmt_tools(mcp: FastMCP) -> None:
    """Read tools always; money-moving tools only when their kill switch is on.

    A disabled tool is never registered, so an agent can't see it in
    list_tools and can't be talked into calling it.
    """

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_order_details(order_id: str) -> dict:
        """Full detail for one order: items+prices, totals, payment method,
        ship-to address, status, tracking numbers, invoice URL, cancel link."""
        try:
            client = get_amazon_client()
            return await client.get_order_details(order_id)
        except Exception as e:
            handle_api_error(e)

    @mcp.tool(annotations={"readOnlyHint": True})
    async def track_shipment(order_id: str) -> dict:
        """Shipment status + tracking numbers for an order."""
        try:
            client = get_amazon_client()
            return await client.track_shipment(order_id)
        except Exception as e:
            handle_api_error(e)

    @mcp.tool(annotations={"readOnlyHint": True})
    async def list_returns() -> dict:
        """List returns/refunds from the Returns Center: order ids, items,
        statuses (Refund issued / Return received / …), refund amounts."""
        try:
            client = get_amazon_client()
            return await client.list_returns()
        except Exception as e:
            handle_api_error(e)

    if constants.RETURNS_ENABLED:
        _register_initiate_return(mcp)
    if constants.ORDER_CANCEL_ENABLED:
        _register_cancel_order(mcp)
    if constants.ORDERING_ENABLED:
        _register_checkout(mcp)


def _register_initiate_return(mcp: FastMCP) -> None:
    @mcp.tool(
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}
    )
    async def initiate_return(
        order_id: str,
        reason: RETURN_REASON = "no-longer-needed",
        asin_hint: str | None = None,
    ) -> dict:
        """Start a return/refund for an order. Multi-item orders: set asin_hint
        to the ASIN (or part of the item name) to pick the right line. Reason is
        a friendly code like 'defective' or 'wrong-item'. Verify afterwards with
        list_returns; if Amazon demands an extra step, finish in the Returns Center."""
        try:
            _require_write_session()
            client = get_amazon_client()
            return await client.initiate_return(order_id, asin=asin_hint, reason=reason)
        except Exception as e:
            handle_api_error(e)


def _register_cancel_order(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
    async def cancel_order(order_id: str) -> dict:
        """Cancel an unshipped order entirely. Only works before shipment;
        check get_order_details first. Verify with get_orders afterwards."""
        try:
            _require_write_session()
            client = get_amazon_client()
            return await client.cancel_order(order_id)
        except Exception as e:
            handle_api_error(e)


def _register_checkout(mcp: FastMCP) -> None:
    # Not readOnly: it POSTs through checkout steps (address/shipping/payment).
    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def checkout_preview() -> dict:
        """Walk the cart through checkout WITHOUT buying: returns the review-step
        summary (totals, preselected address/payment, verification flags). Show
        this to the user before calling place_order."""
        try:
            _require_write_session()
            client = get_amazon_client()
            return await client.checkout_preview()
        except Exception as e:
            handle_api_error(e)

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    async def place_order(
        confirm: Literal["PLACE_ORDER"],
        address_id: str | None = None,
        payment_id: str | None = None,
    ) -> dict:
        """PLACE THE ORDER and charge the default payment method.

        HARD REQUIREMENT: confirm must be the exact literal "PLACE_ORDER" —
        only pass it after the user has explicitly approved this purchase and
        you have shown them checkout_preview. address_id/payment_id optionally
        override preselected choices (values come from checkout_preview).
        If Amazon demands card verification, status='needs_verification' comes
        back with manual instructions; verify afterwards via get_orders.
        """
        try:
            _require_write_session()
            client = get_amazon_client()
            return await client.place_order(confirm, address_id=address_id, payment_id=payment_id)
        except Exception as e:
            handle_api_error(e)
