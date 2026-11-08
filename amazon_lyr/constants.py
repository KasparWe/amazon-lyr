"""Central constants: endpoints, cookies, retry policy."""

from __future__ import annotations

from typing import Final

# ── Money-movement kill switches ─────────────────────────────────────────
# These are deliberately plain code constants — NOT env vars, CLI flags, or
# tool parameters — so nothing an agent can send over MCP (and no prompt
# injection hidden in a product page or review) can turn them on. Enabling
# one requires editing this file and reinstalling the package.
#
# While False:
#   * the matching MCP tools are not registered at all (agents can't see them),
#   * the AmazonClient methods refuse before making any request, and
#   * the transport layer refuses any request that looks like checkout /
#     order placement / buy-now (see api_client._guard_request).
ORDERING_ENABLED: Final[bool] = False  # checkout_preview + place_order
ORDER_CANCEL_ENABLED: Final[bool] = False  # cancel_order
RETURNS_ENABLED: Final[bool] = False  # initiate_return

SEARCH_PATH = "/s"
PRODUCT_PATH = "/dp"
REVIEWS_PATH = "/product-reviews"
ORDERS_PATH = "/your-orders/orders"
ORDER_DETAILS_PATH = "/gp/your-account/order-details"
RETURNS_PATH = "/returns/orders"
RETURN_START_PATH = "/gp/orc/return/start"
CART_ADD_PATH = "/cart/add-to-cart"
CART_VIEW_PATH = "/gp/cart/view.html"
CHECKOUT_START_PATH = "/gp/checkout/portal/checkout.html"
WISHLIST_PATH = "/hz/wishlist/ls"
WISHLIST_ADD_PATH = "/hz/wishlist/lsadd"
SIGNIN_MARKER = "/ap/signin"

# Checkout workflow safety markers.
CHECKOUT_CONFIRM_LITERAL = "PLACE_ORDER"  # exact string place_order() requires
CHECKOUT_VERIFICATION_MARKERS = (
    "verify your card",
    "one-time password",
    "enter the otp",
    "additional verification",
    "3-d secure",
)
CHECKOUT_SUCCESS_MARKERS = (
    "order placed, thanks",
    "thank you, your order is placed",
    "your order is confirmed",
    "order confirmation",
)

# Friendly names → Amazon return-reason codes (pass-through for unknown codes).
RETURN_REASONS = {
    "no-longer-needed": "NO_LONGER_NEED",
    "defective": "DEFECTIVE_ITEM",
    "damaged": "ITEM_DAMAGED",
    "wrong-item": "WRONG_ITEM_SENT",
    "missing-parts": "MISSING_PARTS",
    "better-price": "BETTER_PRICE",
    "not-as-described": "NOT_AS_DESCRIBED",
    "arrived-too-late": "ARRIVED_TOO_LATE",
    "size-fit-issue": "SIZE_OR_FIT_ISSUE",
    "accidental-order": "ORDERED_BY_MISTAKE",
}

# Captcha page markers (Amazon bot-wall responses).
CAPTCHA_MARKERS = (
    "Enter the characters you see below",
    "Robot Check",
    "api-services-support@amazon.com",
    "To discuss automated access to Amazon data please contact",
)

# Login/session cookie names depend on the storefront — see marketplace.py.

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "en-US,en;q=0.9"

MAX_RETRIES = 3
RETRY_BACKOFF = 1.5
# Live probing showed sub-3s cadences get silent empty grids (soft throttle).
REQUEST_INTERVAL = 3.0
REQUEST_TIMEOUT = 30
