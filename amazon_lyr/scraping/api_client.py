"""Amazon HTML client on curl_cffi with captcha/signin detection and retries."""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

from amazon_lyr import constants
from amazon_lyr.constants import (
    ACCEPT_LANGUAGE,
    CART_ADD_PATH,
    CART_VIEW_PATH,
    CHECKOUT_START_PATH,
    CHECKOUT_VERIFICATION_MARKERS,
    DEFAULT_USER_AGENT,
    MAX_RETRIES,
    ORDER_DETAILS_PATH,
    ORDERS_PATH,
    PRODUCT_PATH,
    REQUEST_INTERVAL,
    REQUEST_TIMEOUT,
    RETRY_BACKOFF,
    RETURN_REASONS,
    RETURN_START_PATH,
    RETURNS_PATH,
    REVIEWS_PATH,
    SEARCH_PATH,
    SIGNIN_MARKER,
    WISHLIST_ADD_PATH,
    WISHLIST_PATH,
)
from amazon_lyr.exceptions import (
    AmazonError,
    CaptchaError,
    NotFoundError,
    OrderingDisabledError,
    ParseError,
    SessionExpiredError,
)
from amazon_lyr.marketplace import DEFAULT_HOST, get_marketplace
from amazon_lyr.ratelimit import RateLimiter
from amazon_lyr.scraping.parsers import (
    _ORDER_ID_RE,
    build_review_insights,
    parse_cancel_form,
    parse_cart,
    parse_checkout_form,
    parse_order_confirmation,
    parse_order_details,
    parse_orders,
    parse_product,
    parse_return_form,
    parse_returns,
    parse_reviews,
    parse_search,
    parse_wishlist,
)


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


def _validate_asin(asin: str) -> str:
    asin = (asin or "").strip().upper()
    if not _ASIN_RE.fullmatch(asin):
        raise AmazonError(
            f"{asin!r} is not a valid ASIN (10 characters, letters/digits — e.g. B08N5WRWNW). "
            "Copy it from search_products results."
        )
    return asin


# Anything that looks like entering checkout or committing a purchase.
# Matched against the request path (all methods) and POST form field names.
_CHECKOUT_RE = re.compile(
    r"checkout|/gp/buy/|place[-_.\s]?(?:your[-_.\s]?)?order|buy[-_.\s]?now|"
    r"handle-buy-box|turbo|one[-_.\s]?click|1[-_.\s]?click",
    re.IGNORECASE,
)
# Cart mutation field names come from parse_cart(); anything else is refused so
# remove_from_cart/update_cart_quantity can't be abused as arbitrary-POST tools.
_CART_DELETE_KEY_RE = re.compile(r"submit\.delete\.[A-Za-z0-9._:-]{1,128}")
_CART_QTY_FIELD_RE = re.compile(r"quantityBox\.[A-Za-z0-9._:-]{1,128}")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 10


def _require_enabled(flag_name: str, action: str) -> None:
    """Refuse a money-moving action unless its code constant is True."""
    if not getattr(constants, flag_name):
        raise OrderingDisabledError(
            f"{action} is disabled in this build ({flag_name} = False in "
            "amazon_lyr/constants.py). Do it in the browser instead."
        )


def _is_place_order_button(btn: dict) -> bool:
    return bool(_CHECKOUT_RE.search(f"{btn.get('name', '')} {btn.get('value', '')}")) and bool(
        re.search(r"place|buy|order", f"{btn.get('name', '')} {btn.get('value', '')}", re.I)
    )


class AmazonClient:
    def __init__(self, cookies: dict[str, str] | None = None, marketplace: str | None = None):
        self._cookies = cookies or {}
        self._market = get_marketplace(marketplace or DEFAULT_HOST)
        self._base = self._market.base_url
        self._session: AsyncSession | None = None
        self._rate_limiter = RateLimiter(min_interval=REQUEST_INTERVAL)

    # ── transport ────────────────────────────────────────────────────────

    async def _get_session(self) -> AsyncSession:
        if self._session is None:
            self._session = AsyncSession(impersonate="chrome", timeout=REQUEST_TIMEOUT)
            for name, value in self._cookies.items():
                self._session.cookies.set(name, value, domain="." + self._market.domain)
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _check_page(self, resp) -> None:
        """Raise mapped errors for captcha / signin / 404 responses."""
        body = resp.text[:20000]
        final_url = str(getattr(resp, "url", ""))
        if any(
            marker in body
            for marker in (
                "Enter the characters you see below",
                "Robot Check",
                "api-services-support@amazon.com",
            )
        ):
            raise CaptchaError("Amazon served a CAPTCHA page")
        if "To discuss automated access to Amazon data please contact" in resp.text:
            raise CaptchaError("Amazon served an automated-access block")
        # Full-text scan (not just the head): login forms can sit deep in the
        # page for account-area routes when the session is dead.
        if SIGNIN_MARKER in final_url or 'action="/ap/signin"' in resp.text:
            raise SessionExpiredError("Amazon redirected to sign-in")
        if resp.status_code == 404:
            raise NotFoundError("Page not found (404)")

    def _guard_request(self, method: str, url: str, data: dict | None) -> None:
        """Last line of defence, independent of which tool/method built the request.

        1. Only https URLs on the configured Amazon marketplace's domain.
        2. While ORDERING_ENABLED is False, refuse anything that looks like
           checkout, order placement, buy-now or 1-click — by URL path for every
           method, and by form field name for POSTs.
        """
        parts = urlsplit(url)
        base_domain = self._market.domain
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not (host == base_domain or host.endswith("." + base_domain)):
            raise AmazonError(f"Refusing request to non-Amazon or non-https URL: {url!r}")
        if not constants.ORDERING_ENABLED:
            if _CHECKOUT_RE.search(parts.path):
                raise OrderingDisabledError(
                    f"Blocked {method} {parts.path}: checkout/ordering is disabled "
                    "(ORDERING_ENABLED = False)."
                )
            if method.upper() != "GET" and data:
                bad = [k for k in data if _CHECKOUT_RE.search(str(k))]
                if bad:
                    raise OrderingDisabledError(
                        f"Blocked {method} with checkout/order fields {bad[:3]}: ordering is "
                        "disabled (ORDERING_ENABLED = False)."
                    )

    async def _send_following_redirects(
        self, session: AsyncSession, method: str, url: str, kwargs: dict
    ) -> Any:
        """Follow redirects by hand so every hop passes _guard_request.

        Automatic redirects would let an allowed URL bounce into checkout or onto
        plain http (the session cookies aren't marked Secure).
        """
        for _ in range(_MAX_REDIRECTS + 1):
            resp = await session.request(method, url, allow_redirects=False, **kwargs)
            location = (getattr(resp, "headers", None) or {}).get("location")
            if resp.status_code not in _REDIRECT_STATUSES or not location:
                return resp
            url = urljoin(url, location)
            if resp.status_code in (301, 302, 303):
                # Browser semantics: the follow-up is a GET without the body.
                method = "GET"
                kwargs = {k: v for k, v in kwargs.items() if k not in ("data", "json", "params")}
            else:
                kwargs = {k: v for k, v in kwargs.items() if k != "params"}
            self._guard_request(method, url, kwargs.get("data"))
        raise AmazonError(f"Too many redirects (>{_MAX_REDIRECTS}) starting from {url!r}")

    async def _request(self, method: str, path_or_url: str, **kwargs) -> Any:
        url = path_or_url if path_or_url.startswith("http") else urljoin(self._base, path_or_url)
        self._guard_request(method, url, kwargs.get("data"))
        session = await self._get_session()
        headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept-Language": ACCEPT_LANGUAGE}
        kwargs.setdefault("headers", {}).update(headers)

        await self._rate_limiter.acquire()
        last_err: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = await self._send_following_redirects(session, method, url, kwargs)
                self._check_page(resp)
                return resp
            except AmazonError:
                # Typed failures (captcha, sign-in, 404, guard refusals) are final.
                raise
            except Exception as e:  # noqa: BLE001 - retry then wrap
                last_err = e
                status = getattr(e, "code", None) or getattr(
                    getattr(e, "response", None), "status_code", None
                )
                if status == 503:
                    raise CaptchaError(f"Amazon returned 503 (likely bot-wall): {e}") from e
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))
                    continue
        raise AmazonError(f"Request failed after {MAX_RETRIES} attempts: {last_err}")

    async def get_html(self, path: str, params: dict | None = None) -> str:
        resp = await self._request("GET", path, params=params)
        return resp.text

    async def post_form(self, path: str, data: dict, referer: str | None = None) -> str:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if referer:
            headers["Referer"] = referer
        resp = await self._request("POST", path, data=data, headers=headers)
        return resp.text

    # ── reads (research) ─────────────────────────────────────────────────

    async def search_products(
        self,
        query: str,
        department: str | None = None,
        sort: str | None = None,
        price_min: int | None = None,
        price_max: int | None = None,
        min_rating: int | None = None,
        page: int = 1,
    ) -> dict:
        params: dict[str, Any] = {"k": query, "page": str(page)}
        if department:
            params["i"] = department
        if sort:
            params["s"] = sort  # e.g. price-asc-rank, price-desc-rank, review-rank, date-desc-rank
        # Independent query params — Amazon supports low-price/high-price standalone.
        if price_min is not None:
            params["low-price"] = str(price_min)
        if price_max is not None:
            params["high-price"] = str(price_max)
        if min_rating:
            star_map = {4: "p_72:1254471011", 3: "p_72:1254477011"}
            if min_rating in star_map:
                params["rh"] = star_map[min_rating]
        html = await self.get_html(SEARCH_PATH, params=params)
        return parse_search(html, page=page)

    async def get_product(self, asin: str) -> dict:
        asin = _validate_asin(asin)
        html = await self.get_html(f"{PRODUCT_PATH}/{asin}", params=None)
        return parse_product(html, asin=asin)

    async def get_reviews(
        self,
        asin: str,
        filter_by_star: str
        | None = None,  # one_star|two_stars|three_stars|four_stars|five_stars|critical|positive
        sort_by: str = "recent",  # recent|helpful
        page: int = 1,
    ) -> dict:
        asin = _validate_asin(asin)
        params: dict[str, Any] = {"sortBy": sort_by, "pageNumber": str(page)}
        if filter_by_star:
            params["filterByStar"] = filter_by_star
        html = await self.get_html(f"{REVIEWS_PATH}/{asin}", params=params)
        return parse_reviews(html, asin=asin)

    async def get_wishlist(self, wishlist_id: str | None = None) -> dict:
        params = {"cid": wishlist_id} if wishlist_id else None
        html = await self.get_html(WISHLIST_PATH, params=params)
        return parse_wishlist(html)

    async def get_orders(self, status_filter: str | None = None) -> dict:
        params = {}
        if status_filter:
            params = {"orderFilter": status_filter}  # e.g. last30, months-3, pending, shipped
        html = await self.get_html(ORDERS_PATH, params=params)
        return parse_orders(html)

    # ── writes (shopping) ────────────────────────────────────────────────

    async def add_to_cart(self, asin: str, quantity: int = 1) -> dict:
        asin = _validate_asin(asin)
        # Single source of truth for clamping — tools and CLI both route here.
        quantity = max(1, min(int(quantity or 1), 30))
        data = {
            "ASIN.1": asin,
            "quantity.1": str(quantity),
            "submit.addToCart": "Add to Cart",
        }
        html = await self.post_form(CART_ADD_PATH, data=data)
        cart = parse_cart(html)
        return {"status": "ok", "requested": {"asin": asin, "quantity": quantity}, "cart": cart}

    async def view_cart(self) -> dict:
        html = await self.get_html(CART_VIEW_PATH)
        return parse_cart(html)

    async def remove_from_cart(self, item_key: str) -> dict:
        # item_key comes from view_cart() items[].key (e.g. submit.delete.<ID>)
        if not _CART_DELETE_KEY_RE.fullmatch(item_key or ""):
            raise AmazonError(
                f"{item_key!r} is not a cart delete key (expected submit.delete.<id> from view_cart)."
            )
        data = {item_key: "Delete"}
        html = await self.post_form(CART_VIEW_PATH, data=data)
        cart = parse_cart(html)
        removed = all(i.get("key") != item_key for i in cart.get("items", []))
        return {"status": "ok" if removed else "uncertain", "cart": cart}

    async def update_cart_quantity(self, qty_field: str, quantity: int) -> dict:
        """Change an item's quantity. qty_field comes from view_cart items[].qty_field."""
        if not _CART_QTY_FIELD_RE.fullmatch(qty_field or ""):
            raise AmazonError(
                f"{qty_field!r} is not a cart quantity field (expected quantityBox.<id> "
                "from view_cart)."
            )
        quantity = max(1, min(int(quantity or 1), 30))
        data = {qty_field: str(quantity)}
        html = await self.post_form(CART_VIEW_PATH, data=data)
        cart = parse_cart(html)
        applied = any(
            i.get("qty_field") == qty_field and i.get("quantity") == quantity
            for i in cart.get("items", [])
        )
        return {
            "status": "ok" if applied else "uncertain",
            "requested": {"qty_field": qty_field, "quantity": quantity},
            "cart": cart,
        }

    async def add_to_wishlist(self, asin: str) -> dict:
        """Add an ASIN to the default wishlist via the /hz/wishlist/lsadd flow.

        Best-effort by design: needs a CSRF token discovered from the wishlist
        page; raises ParseError with manual guidance when Amazon withholds it.
        """
        asin = _validate_asin(asin)
        ls_html = await self.get_html(WISHLIST_PATH, params={"asin": asin})
        token = self._extract_csrf_token(ls_html)

        data = {"asin": asin, "csrfToken": token, "ref_": "lyr_add"}
        resp_html = await self.post_form(
            WISHLIST_ADD_PATH, data=data, referer=self._base + WISHLIST_PATH
        )
        low = resp_html.lower()
        if any(m in low for m in ("added to your list", "item added", "added to wishlist")):
            return {"status": "ok", "asin": asin}
        # Verify by membership — some variants return sparse responses.
        wish = await self.get_wishlist()
        if any(i.get("asin") == asin for i in wish.get("items", [])):
            return {"status": "ok", "asin": asin, "verified_by": "wishlist_scan"}
        raise ParseError(
            f"Could not confirm {asin} was added to the wishlist. Verify manually at "
            f"{self._base}/hz/wishlist/ls — the list-add flow may have changed."
        )

    @staticmethod
    def _extract_csrf_token(html: str) -> str:
        """CSRF tokens appear as hidden inputs or embedded JSON on wishlist pages."""
        soup = BeautifulSoup(html, "html.parser")
        node = soup.select_one('input[name="anti-csrftoken-a2z"], input[name="csrfToken"]')
        if node and node.get("value"):
            return str(node["value"])
        match = re.search(r'"csrfToken"\s*:\s*"([^"]{16,})"', html) or re.search(
            r'anti-csrftoken-a2z(?:%3A|=|%3D|["\s:=]+)([A-Za-z0-9+/_-]{32,})', html
        )
        if match:
            return match.group(1)
        raise ParseError(
            "No CSRF token found on the wishlist page — sign-in cookies may be stale "
            "(re-run `amazon-lyr login`) or the page layout changed."
        )

    # ── checkout & ordering ──────────────────────────────────────────────

    async def _initiate_checkout(self) -> str:
        """Proceed from cart into the checkout workflow; returns first step HTML."""
        _require_enabled("ORDERING_ENABLED", "Checkout")
        cart = await self.view_cart()
        if cart.get("empty") or not cart.get("items"):
            raise AmazonError("Cart is empty — add items with add_to_cart first.")
        return await self.post_form(
            CART_VIEW_PATH,
            data={"submit.checkout": "Proceed to checkout"},
            referer=self._base + CART_VIEW_PATH,
        )

    async def checkout_preview(self) -> dict:
        """Walk the checkout workflow WITHOUT placing anything; stop at the final
        review step and summarize what an order would look like."""
        _require_enabled("ORDERING_ENABLED", "Checkout preview")
        html = await self._initiate_checkout()
        for _ in range(8):
            step = parse_checkout_form(html)
            confirmation = parse_order_confirmation(html)
            if confirmation:
                raise ParseError("Unexpected order confirmation during preview — aborting.")
            is_review = step["page_markers"].get("review")
            if is_review and not step["submit_buttons"]:
                break
            if is_review:
                break  # final review reached; do NOT submit further during preview
            # Single-page checkout puts "Place your order" on the first page, and
            # page-marker detection is heuristic — never click a buying button here.
            if any(_is_place_order_button(b) for b in step["submit_buttons"]):
                break
            # Continue with defaults (no selections overridden).
            data = {**step["fields"]}
            for choice in step["choices"]:
                if choice["preselected"]:
                    data[choice["field"]] = choice["value"]
            for sel in step["selects"]:
                pre = next((o["value"] for o in sel["options"] if o["value"]), None)
                if pre:
                    data[sel["field"]] = pre
            if step["submit_buttons"]:
                btn = step["submit_buttons"][0]
                data[btn["name"]] = btn["value"]
            html = await self.post_form(
                step["action"] or CHECKOUT_START_PATH,
                data=data,
                referer=self._base + CART_VIEW_PATH,
            )
        step = parse_checkout_form(html)
        items = await self.view_cart()
        verification = [m for m in CHECKOUT_VERIFICATION_MARKERS if m in html.lower()]
        return {
            "stage": "review",
            "totals": step.get("totals"),
            "choices": [c for c in step["choices"] if c["preselected"]] or None,
            "cart_items": len(items.get("items", [])),
            "verification_required_markers": verification or None,
            "note": "Preview only — nothing was ordered. Use place_order to actually buy.",
        }

    async def place_order(
        self, confirm: str, address_id: str | None = None, payment_id: str | None = None
    ) -> dict:
        """Complete checkout and place the order. Requires confirm='PLACE_ORDER'
        (exact literal) so this can never fire accidentally."""
        from amazon_lyr.constants import CHECKOUT_CONFIRM_LITERAL

        _require_enabled("ORDERING_ENABLED", "Placing orders")
        if confirm != CHECKOUT_CONFIRM_LITERAL:
            raise AmazonError(
                f"Refusing to place an order: pass confirm='{CHECKOUT_CONFIRM_LITERAL}' "
                "(exact string) after the user has explicitly approved the purchase. "
                "Run checkout_preview first and show the user the totals."
            )
        html = await self._initiate_checkout()
        placed: dict | None = None
        for _ in range(10):
            confirmation = parse_order_confirmation(html)
            if confirmation:
                placed = confirmation
                break
            low = html.lower()
            if any(m in low for m in CHECKOUT_VERIFICATION_MARKERS):
                return {
                    "status": "needs_verification",
                    "note": "Amazon demanded card verification (OTP/3-D Secure). Finish "
                    f"in the browser at {self._base}/gp/cart/view.html then "
                    "check get_orders.",
                }
            step = parse_checkout_form(html)
            data = {**step["fields"]}
            overrides = {
                "address": address_id,
                "payment": payment_id,
            }
            for choice in step["choices"]:
                field_kind = next((k for k in overrides if k in choice["field"].lower()), None)
                wanted = overrides.get(field_kind) if field_kind else None
                if wanted and str(choice["value"]) == wanted:
                    data[choice["field"]] = choice["value"]
                elif choice["preselected"]:
                    data[choice["field"]] = choice["value"]
            for sel in step["selects"]:
                pre = next((o["value"] for o in sel["options"] if o["value"]), None)
                if pre:
                    data[sel["field"]] = pre
            if step["submit_buttons"]:
                btn = step["submit_buttons"][0]
                data[btn["name"]] = btn["value"]
            html = await self.post_form(
                step["action"] or CHECKOUT_START_PATH,
                data=data,
                referer=self._base + CART_VIEW_PATH,
            )
        else:
            raise ParseError(
                "Checkout workflow did not reach a confirmation within 10 steps. "
                "Nothing appears to have been ordered — verify at "
                f"{self._base}/gp/your-orders/orders before retrying."
            )
        result = {"status": "placed", **placed}
        if not placed.get("order_id"):
            result["note"] = (
                "Order appears placed but no confirmation number was parsed. "
                "Verify with get_orders."
            )
        return result

    # ── order management ─────────────────────────────────────────────────

    async def get_order_details(self, order_id: str) -> dict:
        if not _ORDER_ID_RE.fullmatch(order_id.strip()):
            raise AmazonError(
                f"{order_id!r} is not a valid Amazon order id (format: 123-1234567-1234567)."
            )
        html = await self.get_html(ORDER_DETAILS_PATH, params={"orderID": order_id.strip()})
        return parse_order_details(html, order_id=order_id.strip(), base=self._base)

    async def track_shipment(self, order_id: str) -> dict:
        details = await self.get_order_details(order_id)
        return {
            "order_id": details["order_id"],
            "status": details.get("status"),
            "tracking_numbers": details.get("tracking_numbers"),
            "ship_to": details.get("ship_to"),
            "note": None
            if details.get("tracking_numbers")
            else "No tracking found on the order page — it may not have shipped yet, "
            "or the carrier page is separate. Check the invoice_url for full detail.",
        }

    async def list_returns(self) -> dict:
        html = await self.get_html(RETURNS_PATH)
        parsed = parse_returns(html)
        # Sign-out detection is centralized in _check_page (form-action marker),
        # so a plain empty result here is just an empty returns list.
        return parsed

    async def initiate_return(
        self, order_id: str, asin: str | None = None, reason: str = "no-longer-needed"
    ) -> dict:
        """Drive Amazon's multi-step return wizard as far as scraping allows.

        Best-effort by design: Amazon rotates wizard tokens aggressively. Every
        failure mode raises a ParseError with manual-fallback guidance.
        """
        _require_enabled("RETURNS_ENABLED", "Starting returns")
        if not _ORDER_ID_RE.fullmatch(order_id.strip()):
            raise AmazonError(f"Invalid order id {order_id!r} (expected 123-1234567-1234567).")
        oid = order_id.strip()

        # Step 1: load the item-selection form for this order.
        try:
            start_html = await self.get_html(RETURN_START_PATH, params={"orderId": oid})
            form = parse_return_form(start_html)
        except ParseError:
            raise
        except Exception as e:
            raise ParseError(f"Could not open the return flow for {oid}: {e}") from e

        if not form["items"]:
            raise ParseError("No selectable items found on the return form.")

        # Pick the requested item, or the first one.
        chosen = form["items"][0]
        if asin:
            matches = [i for i in form["items"] if asin.lower() in str(i["label"]).lower()]
            if not matches:
                raise AmazonError(
                    f"Item {asin} not found on the return form for {oid}. "
                    f"Available: {[i['label'] for i in form['items']][:5]}"
                )
            chosen = matches[0]

        # Step 2: submit item + hidden fields (+ reason when the select exists).
        data = {**form["fields"], chosen["field"]: chosen["value"]}
        reason_code = RETURN_REASONS.get(reason, reason)
        reason_field = form.get("reason_field")
        if reason_field:
            opts = {o["value"]: (o["label"] or "").lower() for o in form["reason_options"]}
            if reason_code in opts:
                resolved_reason = reason_code
            else:
                # Friendly name matched against option labels ("defective" → DEFECTIVE_ITEM).
                resolved_reason = next(
                    (v for v, lbl in opts.items() if reason.replace("-", " ") in lbl),
                    next(iter(opts), None),
                )
            if resolved_reason:
                data[reason_field] = resolved_reason
        action = form["action"] or RETURN_START_PATH
        confirm_html = await self.post_form(action, data=data, referer=self._base + RETURNS_PATH)

        # Step 3 heuristic: confirmation pages mention refund/return summary.
        blob = confirm_html.lower()
        submitted = any(
            m in blob for m in ("your return is started", "refund", "print label", "return summary")
        )
        return {
            "status": "submitted" if submitted else "in_progress",
            "order_id": oid,
            "item": chosen["label"],
            "reason": reason,
            "reason_submitted": data.get(reason_field) if reason_field else None,
            "next_step": None
            if submitted
            else (
                "Amazon required an extra confirmation step this session. Finish at "
                f"{self._base}/returns/orders — then verify with list_returns."
            ),
        }

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel an unshipped order via the two-step cancellation flow."""
        _require_enabled("ORDER_CANCEL_ENABLED", "Cancelling orders")
        details = await self.get_order_details(order_id)
        cancel_url = details.get("cancel_url")
        if not cancel_url:
            raise AmazonError(
                f"No cancellation option found for {order_id}. Orders can usually only "
                "be cancelled before they ship — check get_order_details for status."
            )

        page_html = await self.get_html(cancel_url)
        form = parse_cancel_form(page_html)

        data = {**form["fields"]}
        for box in form["item_checkboxes"]:
            data[box["field"]] = box["value"]
        data.setdefault("submit.save", "Cancel checked items")

        confirm_html = await self.post_form(
            form["action"] or cancel_url, data=data, referer=cancel_url
        )
        blob = confirm_html.lower()
        cancelled = "has been cancelled" in blob or "cancellation" in blob and "confirmed" in blob
        return {
            "status": "cancelled" if cancelled else "uncertain",
            "order_id": order_id,
            "note": None
            if cancelled
            else (
                "Cancellation may need one more confirmation step — verify with "
                "get_orders before assuming failure."
            ),
        }

    # ── review research ──────────────────────────────────────────────────

    async def fetch_all_reviews(self, asin: str, max_pages: int = 3, **kwargs) -> list[dict]:
        """Collect up to max_pages of reviews, stopping early when exhausted."""
        all_reviews: list[dict] = []
        seen_ids: set[str] = set()
        for page in range(1, max(1, min(max_pages, 10)) + 1):
            result = await self.get_reviews(asin, page=page, **kwargs)
            batch = result.get("reviews", [])
            new = [r for r in batch if r.get("id") and r["id"] not in seen_ids]
            if not new:
                break
            seen_ids.update(r["id"] for r in new)
            all_reviews.extend(new)
            await asyncio.sleep(REQUEST_INTERVAL)
        return all_reviews

    async def review_insights(self, asin: str, max_pages: int = 3) -> dict:
        """Aggregate review research: distribution, verified %, keyword themes."""
        reviews = await self.fetch_all_reviews(asin, max_pages=max_pages)
        insights = build_review_insights(reviews)
        insights["asin"] = asin
        insights["pages_fetched"] = max(1, min(max_pages, 10))
        return insights

    async def search_in_reviews(self, asin: str, query: str, max_pages: int = 3) -> dict:
        """Fetch several pages of reviews and keep those mentioning `query`."""
        query_lower = query.lower().strip()
        if not query_lower:
            raise ValueError("query must not be empty")
        reviews = await self.fetch_all_reviews(asin, max_pages=max_pages)
        hits = [
            r
            for r in reviews
            if query_lower in (r.get("title") or "").lower()
            or query_lower in (r.get("body") or "").lower()
        ]
        insights = build_review_insights(hits)
        return {
            "asin": asin,
            "query": query,
            "matches": len(hits),
            "of_scanned": len(reviews),
            "average_rating_of_matches": insights["average_rating"],
            "reviews": hits[:20],
        }
