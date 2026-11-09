"""HTML → normalized dicts for search, product, reviews, cart, orders, wishlist.

All parsers are defensive: missing selectors yield None / [] instead of raising,
so partial data beats a crashed tool.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from amazon_lyr.exceptions import ParseError

_PRICE_RE = re.compile(r"[\d,]+\.?\d*")
_ORDER_ID_RE = re.compile(r"\d{3}-\d{7}-\d{7}")
_THUMB_RE = re.compile(r"\._.*?_\.")  # matches ._AC_UY218_. etc. (multi-segment)
_RATING_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:out of|/)\s*5\b")
# Some geo/bot variants strip .a-price markup from the grid but leave the
# amount as visible text. ponytail: heuristic scan; get_product is authoritative.
_MONEY_RE = re.compile(
    r"(?:CDN\$\s?|C\$\s?|[$€£₹¥]|USD\s?|CAD\s?|EUR\s?|GBP\s?|INR\s?)"
    r"[0-9][0-9,\u00a0]*(?:\.[0-9]{2})?"
)


_CURRENCY_RE = re.compile(r"^([^\d\s.,][^\s\d.,]*)")


def _extract_currency(price_text: str | None) -> str | None:
    if not price_text:
        return None
    match = _CURRENCY_RE.match(price_text.strip())
    symbol = match.group(1) if match else None
    # Only trust short symbols/codes ($, £, INR, USD); not stray words.
    return symbol if symbol and len(symbol) <= 4 else None


def _scan_price_text(node: Tag) -> str | None:
    """Last-resort price extraction: first decimal-bearing money token in card text.

    ponytail: trusts DOM order (live buy boxes render the sale price first);
    coupon/list-price mispicks are possible — get_product remains authoritative.
    """
    best: str | None = None
    for match in _MONEY_RE.finditer(node.get_text(" ", strip=True)):
        token = match.group(0).strip()
        if "." in token:
            return token
        best = best or token
    return best


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _text(node: Tag | None) -> str | None:
    if node is None:
        return None
    text = node.get_text(" ", strip=True)
    return text or None


def _parse_price(text: str | None) -> float | None:
    if not text:
        return None
    match = _PRICE_RE.search(text.replace("\u00a0", " "))
    if not match:
        return None
    try:
        return float(Decimal(match.group(0).replace(",", "")))
    except InvalidOperation:
        return None


def _rating_from_text(text: str | None) -> float | None:
    if not text:
        return None
    match = _RATING_RE.search(text)
    return float(match.group(1)) if match else None


# ── search ───────────────────────────────────────────────────────────────


def parse_search(html: str, page: int = 1) -> dict:
    soup = _soup(html)
    results: list[dict] = []
    for card in soup.select('div[data-component-type="s-search-result"]'):
        asin = card.get("data-asin", "")
        if not asin:
            continue
        title = _text(card.select_one("h2 span"))
        price_node = card.select_one(".a-price .a-offscreen")
        price = _text(price_node) or _scan_price_text(card)
        rating_text = _text(
            card.select_one("span.a-icon-alt") or card.select_one("i.a-icon-star-small")
        )
        ratings_count = _text(card.select_one('span[aria-label$="ratings"]'))
        if not ratings_count:
            rc = card.select_one("a.s-underline-link span")
            m = re.search(r"[\d,]+", rc.get_text() if rc else "")
            ratings_count = m.group(0) if m else None
        img = card.select_one("img.s-image")
        results.append(
            {
                "asin": asin,
                "title": title,
                "price": price,
                "price_value": _parse_price(price),
                "currency": _extract_currency(price),
                "rating": _rating_from_text(rating_text),
                "ratings_count": ratings_count,
                "sponsored": bool(
                    card.select_one(
                        'div[data-component-type="sp-sponsored-result"], .puis-label-popover-default + .a-color-secondary'
                    )
                ),
                "image_url": img.get("src") if img else None,
            }
        )
    total_text = _text(soup.select_one('h2[class*="widget-header"] span, .s-breadcrumb span'))
    # Amazon soft-throttles with silent empty grids — distinguish them from
    # legit no-results pages so agents can retry instead of trusting the empty.
    legit_no_results = bool(
        soup.select_one(
            'div.s-no-results-result, [data-component-type="s-no-result"], div.s-result-list .s-no-outline'
        )
    )
    return {
        "query_page": page,
        "total_results_text": total_text,
        "results": results,
        "possibly_throttled": not results and not legit_no_results,
    }


# ── product page ─────────────────────────────────────────────────────────


def _product_features(soup: BeautifulSoup) -> list[str]:
    bullets = soup.select("#feature-bullets li:not(.aok-hidden) span.a-list-item")
    return [t for t in (_text(li) for li in bullets) if t]


def _product_details(soup: BeautifulSoup) -> dict:
    details: dict[str, str] = {}
    for row in soup.select(
        "#detailBullets_feature_div li, #prodDetails tr, #productDetails_techSpec_section_1 tr"
    ):
        key = _text(row.select_one(".a-text-bold, th"))
        val = _text(row.select_one("span:not(.a-text-bold), td"))
        if key and val:
            details[key.strip().rstrip(":").strip()] = val
    for row in soup.select("#productOverview_feature_div tr, table.a-keyvalue tr"):
        key = _text(row.select_one("td.a-span3, th"))
        val = _text(row.select_one("td.a-span9, td:last-child"))
        if key and val:
            details[key.rstrip(":")] = val
    return details


def parse_product(html: str, asin: str) -> dict:
    soup = _soup(html)
    title = _text(soup.select_one("#productTitle"))
    # Some geo variants render prices outside .a-price — scope-scan the buy box.
    price_scope = soup.select_one("#apex_desktop") or soup.select_one("#centerCol") or soup
    price_node = price_scope.select_one(".a-price .a-offscreen") or soup.select_one(
        ".a-price .a-offscreen"
    )
    price = _text(price_node) or _scan_price_text(price_scope)
    list_price = _text(soup.select_one(".basisPrice .a-offscreen, .a-text-price .a-offscreen"))
    rating_text = _text(
        soup.select_one("#acrPopover .a-icon-alt") or soup.select_one("#acrPopover")
    )
    ratings_count_text = _text(soup.select_one("#acrCustomerReviewText"))
    reviews_count: int | None = None
    if ratings_count_text:
        count_match = re.search(r"[\d,]+", ratings_count_text)  # e.g. "(1,755)"
        if count_match:
            reviews_count = int(count_match.group().replace(",", ""))

    availability = _text(soup.select_one("#availability")) or _text(
        soup.select_one("#availabilityInsideBuyBox_feature_div")
    )
    seller = _text(soup.select_one("#sellerProfileTriggerId"))
    brand = _text(soup.select_one("#bylineInfo"))

    images: list[str] = []
    for img in soup.select("#altImages img[src]")[:8]:
        src = str(img.get("src", ""))
        # Thumbnails are small; upscale the size segment.
        images.append(_THUMB_RE.sub("._AC_SL1000_.", src))

    return {
        "asin": asin,
        "title": title,
        "brand": brand,
        "byline": brand,
        "price": price,
        "price_value": _parse_price(price),
        "list_price": list_price,
        "list_price_value": _parse_price(list_price),
        "rating": _rating_from_text(rating_text),
        "reviews_count": reviews_count,
        "availability": availability,
        "buybox_seller": seller,
        "features": _product_features(soup),
        "details": _product_details(soup),
        "images": images,
    }


# ── reviews ──────────────────────────────────────────────────────────────


def parse_reviews(html: str, asin: str) -> dict:
    soup = _soup(html)
    reviews: list[dict] = []
    for block in soup.select('div[data-hook="review"]'):
        review_id = block.get("id", "")
        title = _text(
            block.select_one(
                'a[data-hook="review-title"] span:last-child, [data-hook="review-title"]'
            )
        )
        body = _text(block.select_one('span[data-hook="review-body"]'))
        rating_text = _text(
            block.select_one(
                "[data-hook=review-star-rating] .a-icon-alt, [data-hook=cmps-review-star-rating] .a-icon-alt"
            )
        )
        date = _text(block.select_one("[data-hook=review-date]"))
        author_node = block.select_one(".a-profile-name")
        verified = bool(block.select_one('[data-hook="avp-badge"]'))
        reviews.append(
            {
                "id": review_id,
                "rating": _rating_from_text(rating_text),
                "title": title,
                "body": body[:2000] if body else None,
                "date": date,
                "author": _text(author_node),
                "verified_purchase": verified,
            }
        )

    histogram: dict[str, int | None] = {}
    for row in soup.select('table[data-hook="histogramTable"] tr'):
        star_text = _text(row.select_one("td.a-text-right"))
        pct_text = _text(row.select_one("span[aria-hidden], td.a-text-right + td"))
        if star_text and pct_text:
            pct_match = re.search(r"(\d+)%", pct_text)
            histogram[star_text.strip()] = int(pct_match.group(1)) if pct_match else None

    return {
        "asin": asin,
        "total_reviews_on_page": len(reviews),
        "rating_histogram_percent": histogram or None,
        "reviews": reviews,
    }


# ── cart ─────────────────────────────────────────────────────────────────


_ITEM_QTY_FIELD_RE = re.compile(r"quantityBox\.[A-Za-z0-9._:-]+")


def _parse_cart_rows(soup: BeautifulSoup, rows: list[Tag]) -> dict:
    """Current layout: one div.sc-list-item per line with data-* attributes."""
    active: list[dict] = []
    saved: list[dict] = []
    for row in rows:
        delete = row.select_one('input[name^="submit.delete"]')
        price_text = _text(row.select_one(".a-price .a-offscreen"))
        entry = {
            "key": str(delete.get("name")) if delete else None,
            "asin": row.get("data-asin") or None,
            "name": _text(row.select_one("span.a-truncate-full, .sc-product-title")),
            "price": price_text,
            "price_value": _parse_price(price_text),
        }
        if row.get("data-itemtype") == "saved":
            saved.append(entry)
            continue
        qty_node = row.select_one('[name^="quantityBox"]')
        qty_name = str(qty_node.get("name")) if qty_node else ""
        qty_raw = str(row.get("data-quantity") or "")
        entry.update(
            {
                # Only per-item fields can be POSTed; the bare "quantityBox" of the
                # JS-driven layout can't say which line it belongs to.
                "qty_field": qty_name if _ITEM_QTY_FIELD_RE.fullmatch(qty_name) else None,
                "quantity": int(qty_raw) if qty_raw.isdigit() else None,
                "selected": row.get("data-isselected") != "0",
            }
        )
        active.append(entry)

    subtotal = _text(soup.select_one("#sc-subtotal-amount-buybox .sc-price"))
    result = {
        "empty": not active,
        "item_count": len(active),
        "items": active,
        "subtotal": subtotal,
        "subtotal_value": _parse_price(subtotal),
        "saved_for_later": saved,
    }
    if any(not i["selected"] for i in active):
        result["subtotal_note"] = (
            "Amazon's subtotal only counts ticked items; unticked items have selected=false."
        )
    return result


def parse_add_to_cart_form(html: str) -> dict | None:
    """Fields for /cart/add-to-cart from a product page's one-time-purchase buy box.

    Only the add-to-cart identifiers are returned — never the buy-now fields.
    None when the page has no add-to-cart offer (unavailable, variant not chosen).
    """
    soup = _soup(html)
    forms = soup.select("form#addToCart")
    form = next((f for f in forms if not f.select_one('[name*="recurringDelivery"]')), None)
    if form is None or not form.select_one('[name="submit.add-to-cart"]'):
        return None

    def value(name: str) -> str | None:
        node = form.select_one(f'input[name="{name}"]')
        return str(node.get("value")) if node and node.get("value") else None

    offer = value("items[0.base][offerListingId]") or value("offerListingID")
    token = value("anti-csrftoken-a2z")
    if not offer or not token:
        return None
    return {"csrf": token, "offer_listing_id": offer, "client_name": value("clientName")}


def parse_cart_form_for(html: str, field: str) -> dict | None:
    """Action + CSRF token of the cart form that contains ``field`` (e.g. a delete key)."""
    soup = _soup(html)
    node = soup.find("input", attrs={"name": field})
    if node is None:
        return None
    form = node.find_parent("form")
    if form is None:  # older layouts posted straight to the cart page
        return {"action": "/gp/cart/view.html", "csrf": None}
    token = form.find("input", attrs={"name": "anti-csrftoken-a2z"})
    return {
        "action": str(form.get("action") or "/gp/cart/view.html"),
        "csrf": str(token.get("value")) if token and token.get("value") else None,
    }


def parse_cart(html: str) -> dict:
    soup = _soup(html)
    rows = soup.select("div.sc-list-item[data-asin][data-itemid]")
    if rows:
        return _parse_cart_rows(soup, rows)
    items: list[dict] = []
    for block in soup.select(
        'div.sc-list-item-content, div[data-name="Active Items"] .sc-list-item'
    ):
        form_input = block.select_one('input[name^="submit.delete."]')
        # Quantity lives on an input OR a select; capture the field name for updates.
        qty_select = block.select_one('select[name^="quantityBox."]')
        qty_text_input = block.select_one('input[name^="quantityBox."]')
        if qty_select is not None:
            qty_field = str(qty_select.get("name"))
            selected = qty_select.select_one("option[selected]") or qty_select.select_one(
                'option[value]:not([value=""])'
            )
            qty_raw = selected.get("value") if selected else None
        elif qty_text_input is not None:
            qty_field = str(qty_text_input.get("name"))
            qty_raw = qty_text_input.get("value")
        else:
            qty_field = None
            qty_raw = None
        name = _text(block.select_one(".sc-product-link, h4 a, span.a-truncate-full"))
        price_text = _text(block.select_one(".sc-product-price, .sc-price"))
        items.append(
            {
                "key": form_input.get("name") if form_input else None,
                "qty_field": qty_field,
                "name": name,
                "price": price_text,
                "price_value": _parse_price(price_text),
                "quantity": int(str(qty_raw)) if qty_raw and str(qty_raw).isdigit() else None,
            }
        )

    subtotal = _text(
        soup.select_one(
            "#sc-subtotal-amount-buybox .sc-price, #sc-active-cart .sc-subtotal .sc-price"
        )
    )
    empty = bool(soup.select_one("#sc-empty-cart, .sc-your-amazon-empty-cart-message-container"))

    return {
        "empty": empty,
        "item_count": len(items),
        "items": items,
        "subtotal": subtotal,
        "subtotal_value": _parse_price(subtotal),
    }


# ── orders ───────────────────────────────────────────────────────────────


def parse_orders(html: str) -> dict:
    soup = _soup(html)
    orders: list[dict] = []
    for card in soup.select("div.order-card, div.js-order-card"):
        text_blob = card.get_text(" ", strip=True)
        order_id_match = _ORDER_ID_RE.search(text_blob)
        placed = _text(card.select_one(".order-info span.label")) or None
        header_cols = [_text(c) for c in card.select(".order-header .a-column span")]
        total = next((t for t in header_cols if t and "$" in t), None)
        # Current layout: label/value pairs in li.order-header__header-list-item.
        fields: dict[str, str | None] = {}
        for li in card.select("li.order-header__header-list-item"):
            label = _text(li.select_one(".a-text-caps"))
            value = _text(li.select_one(".a-size-base, .yohtmlc-recipient, .a-popover-trigger"))
            if label:
                fields[label.lower()] = value
        if fields:
            placed = "Order placed"
            header_cols = [fields.get("order placed"), fields.get("total"), fields.get("ship to")]
            total = fields.get("total")
        items: list[dict] = []
        for item in card.select(".yohtmlc-item a, .a-fixed-left-grid .yohtmlc-product-title"):
            name = _text(item)
            if name:
                items.append({"name": name})
        status = _text(
            card.select_one(
                ".delivery-box .yohtmlc-delivery-status, .shipment .js-shipment-status, [data-testid='primary-status']"
            )
        )
        if status is None:
            low = text_blob.lower()
            for marker, label in (
                ("delivered", "Delivered"),
                ("shipped", "Shipped"),
                ("cancelled", "Cancelled"),
                ("pending", "Pending"),
                ("return", "Returned"),
            ):
                if marker in low:
                    status = label
                    break
        orders.append(
            {
                "order_id": order_id_match.group(0) if order_id_match else None,
                "placed_label": placed,
                "placed_date": header_cols[0] if len(header_cols) > 0 else None,
                "total": total,
                "ship_to": header_cols[-1] if len(header_cols) > 2 else None,
                "status": status,
                "items": items[:5],
            }
        )
    return {"order_count": len(orders), "orders": orders}


# ── wishlist ─────────────────────────────────────────────────────────────


def parse_wishlist(html: str) -> dict:
    soup = _soup(html)
    items: list[dict] = []
    blocks = soup.select("li.g-item-sortable") or soup.select("div.g-item-block")
    for block in blocks:
        link = block.select_one('a[id^="itemName_"]')
        price_text = _text(
            block.select_one("span.a-price .a-offscreen, .itemPriceRow .a-color-price")
        )
        asin_match = (
            re.search(r"/dp/([A-Z0-9]{10})", str(link.get("href", "")) if link else "")
            or re.search(r"ASIN:([A-Z0-9]{10})", str(block.get("data-reposition-action-params")))
            or re.search(r"itemName_([A-Z0-9]{10})$", str(link.get("id")) if link else "")
        )
        entry = {
            "asin": asin_match.group(1) if asin_match else None,
            "name": _text(link),
            "price": price_text,
            "price_value": _parse_price(price_text),
        }
        if block.get("data-itemid"):
            entry["item_id"] = str(block.get("data-itemid"))
        items.append(entry)
    signed_in = soup.select_one("#nav-link-accountList-nav-line-1") is not None
    return {
        "item_count": len(items),
        "items": items,
        "note": None if signed_in else "Wishlist may be empty or require sign-in cookies.",
    }


_LIST_ID_RE = re.compile(r"atwl-link-to-list-([A-Z0-9]{8,20})$")
_PRIVACY_WORDS = {"private", "public", "shared"}


def parse_lists(html: str) -> dict:
    """Your lists from the add-to-list dropdown (atlaapi's ``html``)."""
    soup = _soup(html)
    lists: list[dict] = []
    for link in soup.select('a[id^="atwl-link-to-list-"]'):
        match = _LIST_ID_RE.search(str(link.get("id", "")))
        if not match:
            continue
        col = link.select_one(".a-col-right") or link
        parts = list(col.stripped_strings)
        privacy = None
        if parts and parts[-1].lower() in _PRIVACY_WORDS:
            privacy = parts.pop().lower()
        lists.append(
            {"list_id": match.group(1), "name": " ".join(parts) or None, "privacy": privacy}
        )
    return {"count": len(lists), "lists": lists}


def find_list_widget_csrf(html: str) -> str | None:
    """CSRF token of the product page's add-to-list widget (right after its script)."""
    candidates = list(re.finditer(r'name="anti-csrftoken-a2z" value="([^"]+)"', html))
    for m in candidates:
        if "atwl" in html[max(0, m.start() - 2500) : m.start()]:
            return m.group(1)
    return None


def find_first_csrf(html: str) -> str | None:
    m = re.search(r'name="anti-csrftoken-a2z" value="([^"]+)"', html)
    return m.group(1) if m else None


def parse_list_item_for(html: str, asin: str) -> dict | None:
    """itemId / itemExternalId / title / CSRF token for one ASIN on a list page."""
    soup = _soup(html)
    for block in soup.select("li.g-item-sortable"):
        params = str(block.get("data-reposition-action-params") or "")
        ext = re.search(r'"itemExternalId"\s*:\s*"([^"]+)"', params)
        link = block.select_one('a[id^="itemName_"]')
        href_asin = re.search(r"/dp/([A-Z0-9]{10})", str(link.get("href", "")) if link else "")
        if not (
            (ext and f"ASIN:{asin}" in ext.group(1)) or (href_asin and href_asin.group(1) == asin)
        ):
            continue
        token = block.find("input", attrs={"name": "anti-csrftoken-a2z"})
        if token is None:  # fall back to the nearest token before the item
            token = block.find_previous("input", attrs={"name": "anti-csrftoken-a2z"})
        return {
            "item_id": str(block.get("data-itemid") or "") or None,
            "item_external_id": ext.group(1) if ext else f"ASIN:{asin}",
            "title": (link.get("title") or _text(link)) if link else None,
            "csrf": str(token.get("value")) if token and token.get("value") else None,
        }
    return None


# ── order details / tracking / returns ───────────────────────────────────


def _form_fields(form: Tag) -> dict[str, str]:
    """Collect all hidden inputs of a form for POST replay."""
    fields: dict[str, str] = {}
    for inp in form.select('input[type="hidden"]'):
        name = inp.get("name")
        if name:
            fields[str(name)] = str(inp.get("value", ""))
    return fields


def parse_order_details(
    html: str, order_id: str | None = None, base: str = "https://www.amazon.com"
) -> dict:
    soup = _soup(html)
    text_blob = soup.get_text(" ", strip=True)
    oid_match = _ORDER_ID_RE.search(text_blob)
    resolved_id = order_id or (oid_match.group(0) if oid_match else None)

    items: list[dict] = []
    for block in soup.select(".yo-item-block, #orderDetails .a-fixed-left-grid"):
        name = _text(block.select_one(".yohtmlc-product-title, .a-link-normal"))
        price_text = _text(block.select_one(".a-color-price, .yohtmlc-item-price"))
        qty_match = re.search(r"Qty:\s*(\d+)", block.get_text(" ", strip=True))
        items.append(
            {
                "name": name,
                "price": price_text,
                "price_value": _parse_price(price_text),
                "quantity": int(qty_match.group(1)) if qty_match else None,
            }
        )

    totals: dict[str, float] = {}
    for row in soup.select(".od-subtotals tr, #orderTotals tr"):
        label = _text(row.select_one("td:first-child"))
        value_text = _text(row.select_one("td:last-child"))
        if label and value_text:
            val = _parse_price(value_text)
            if val is not None:
                totals[label.strip().rstrip(":")] = val

    payment = _text(soup.select_one("#payment-method, [data-test-id='payment-method']"))
    address = _text(soup.select_one("#shipToAddressId, .displayAddressDL, #primaryShipToAddr"))
    status = _text(
        soup.select_one("[data-test-id='order-status'], .order-status, .delivery-box__status-text")
    )

    tracking: list[dict] = []
    for el in soup.select("[data-testid='tracking-number'], .tracking-number a"):
        num = _text(el)
        if num:
            tracking.append({"number": num.strip()})

    invoice_link = None
    inv = soup.select_one("a[href*='invoice'], a[href*='/gp/aw/invoice']")
    if inv:
        href = str(inv.get("href", ""))
        invoice_link = urljoin(base + "/", href)

    cancel_url = None
    for a in soup.select("a[href*='cancel']"):
        label = (_text(a) or "").lower()
        if "cancel" in label and a.get("href"):
            href = str(a.get("href", ""))
            cancel_url = urljoin(base + "/", href)
            break

    return {
        "order_id": resolved_id,
        "status": status,
        "items": items,
        "totals": totals or None,
        "payment_method": payment,
        "ship_to": address,
        "tracking_numbers": tracking or None,
        "invoice_url": invoice_link,
        "cancel_url": cancel_url,
    }


def parse_returns(html: str) -> dict:
    soup = _soup(html)
    returns_list: list[dict] = []
    for card in soup.select(".return-item, .a-box.return-card, div[data-test-id='return-card']"):
        blob = card.get_text(" ", strip=True)
        oid = _ORDER_ID_RE.search(blob)
        amount = re.search(r"(?:₹|\$|INR\s?)[0-9][0-9,\u00a0.]*", blob)
        status = None
        for marker in (
            "Refund issued",
            "Refund processed",
            "Return received",
            "Replacement sent",
            "Return started",
            "Picked up",
        ):
            if marker.lower() in blob.lower():
                status = marker
                break
        returns_list.append(
            {
                "order_id": oid.group(0) if oid else None,
                "item_name": _text(card.select_one(".yohtmlc-product-title, .a-link-normal")),
                "status": status,
                "refund_amount": amount.group(0).replace("\u00a0", " ") if amount else None,
            }
        )
    return {"count": len(returns_list), "returns": returns_list}


def parse_return_form(html: str) -> dict:
    """Extract the pieces needed to drive Amazon's multi-step return wizard."""
    soup = _soup(html)
    form = soup.select_one("form[method='post'], form[action*='orc']")
    if form is None:
        raise ParseError(
            "Could not find the return-selection form — Amazon's return flow may have "
            "changed or this order may not be eligible. Check it manually on your "
            "Amazon site under /returns/orders"
        )

    items: list[dict] = []
    for box in form.select('input[type="checkbox"], input[type="radio"]'):
        name = box.get("name")
        if not name:
            continue
        parent = box.find_parent("div") or box.find_parent("li") or box
        label = _text(parent.select_one(".a-label-bold, label")) if parent else None
        items.append({"field": str(name), "value": str(box.get("value", "")), "label": label})

    select = form.select_one("select[name*='reason'], select")
    reasons: list[dict] = []
    if select is not None:
        for opt in select.select("option"):
            reasons.append({"value": str(opt.get("value", "")), "label": _text(opt)})

    return {
        "action": str(form.get("action", "")),
        "fields": _form_fields(form),
        "items": items,
        "reason_field": str(select.get("name")) if select is not None else None,
        "reason_options": reasons,
    }


def parse_cancel_form(html: str) -> dict:
    """Extract what's needed to submit an item-cancellation."""
    soup = _soup(html)
    form = soup.select_one("form[method='post']")
    if form is None:
        raise ParseError("Could not find the cancellation form.")
    checks = [
        {"field": str(b.get("name")), "value": str(b.get("value", ""))}
        for b in form.select('input[type="checkbox"], input[type="radio"]')
        if b.get("name")
    ]
    return {
        "action": str(form.get("action", "")),
        "fields": _form_fields(form),
        "item_checkboxes": checks,
        "confirmation_text": _text(soup.select_one(".a-box-inner h1, h2")),
    }


# ── review research ──────────────────────────────────────────────────────

_STOPWORDS = frozenset(
    "the a an and or but for with this that these those have has had was were is are been be "
    "to of in on at by from as it its i my me we our you your he she they them their not no yes "
    "very just so too also than then when what which who how why can could will would should "
    "out up down over under again more most other some such only own same about after before "
    "there here all any both each few if into through during while get got one two really".split()
)


def build_review_insights(reviews: list[dict]) -> dict:
    """Aggregate parsed reviews into research-ready stats (pure function)."""
    total = len(reviews)
    if total == 0:
        return {
            "total": 0,
            "average_rating": None,
            "star_distribution": None,
            "verified_percent": None,
            "top_keywords": [],
            "critical_keywords": [],
        }

    ratings = [r["rating"] for r in reviews if isinstance(r.get("rating"), (int, float))]
    distribution: dict[str, int] = {}
    for rev in reviews:
        star = rev.get("rating")
        if isinstance(star, (int, float)):
            key = f"{int(star)}star"
            distribution[key] = distribution.get(key, 0) + 1

    verified = sum(1 for rev in reviews if rev.get("verified_purchase"))

    def keywords(subset: list[dict], top_n: int = 12) -> list[dict]:
        counts: dict[str, int] = {}
        for rev in subset:
            text = f"{rev.get('title') or ''} {rev.get('body') or ''}".lower()
            for word in re.findall(r"[a-z][a-z'-]{2,}", text):
                if word not in _STOPWORDS:
                    counts[word] = counts.get(word, 0) + 1
        return [
            {"word": w, "count": c}
            for w, c in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
        ]

    critical = [
        rev for rev in reviews if isinstance(rev.get("rating"), (int, float)) and rev["rating"] <= 3
    ]

    return {
        "total": total,
        "average_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
        "star_distribution": distribution or None,
        "verified_percent": round(verified * 100 / total),
        "top_keywords": keywords(reviews),
        "critical_keywords": keywords(critical),
    }


# ── checkout workflow ────────────────────────────────────────────────────


def parse_checkout_form(html: str) -> dict:
    """Walk one step of Amazon's checkout workflow.

    Returns the form to submit (action + hidden tokens), the selectable
    choices (radio groups / selects with labels), any visible totals, and
    whether this page looks like a final review step.
    """
    soup = _soup(html)
    form = (
        soup.select_one("form[action*='address']")
        or soup.select_one("form[action*='shipoption']")
        or soup.select_one("form[action*='pay']")
        or soup.select_one("form[action*='review']")
        or soup.select_one("form[method='post']")
    )
    if form is None:
        raise ParseError(
            "Could not find a checkout form — the flow may have changed, or the cart "
            "is empty. Check your cart on your Amazon site (/gp/cart/view.html)"
        )

    choices: list[dict] = []
    for box in form.select('input[type="radio"]'):
        name = box.get("name")
        if not name:
            continue
        parent = box.find_parent("div") or box.find_parent("li") or box
        label = (
            _text(parent.select_one("label, .a-label-bold, span.a-declarative")) if parent else None
        )
        checked = "checked" in (box.attrs or {})
        choices.append(
            {
                "kind": "radio",
                "field": str(name),
                "value": str(box.get("value", "")),
                "label": label,
                "preselected": bool(checked),
            }
        )

    selects: list[dict] = []
    for sel in form.select("select"):
        options = [
            {"value": str(o.get("value", "")), "label": _text(o)} for o in sel.select("option")
        ]
        selects.append(
            {
                "field": str(sel.get("name")),
                "options": options,
                "preselected": next(
                    (o["value"] for o in options if "selected" in (o.get("_attrs") or {})), None
                ),
            }
        )

    totals_text = _text(soup.select_one(".grand-total-price, #order-summary .a-row:last-child"))

    return {
        "action": str(form.get("action", "")),
        "fields": _form_fields(form),
        "choices": choices,
        "selects": selects,
        "submit_buttons": [
            {"name": str(b.get("name")), "value": str(b.get("value", ""))}
            for b in form.select('input[type="submit"], button[type="submit"]')
            if b.get("name")
        ],
        "totals": totals_text,
        "page_markers": {
            "address": soup.select_one("#ship-to-addressId, [data-test-id='address']") is not None
            or "shipping address" in html.lower()[:20000]
            or "/checkout/address" in str(form.get("action", "")).lower(),
            "payment": "payment method" in html.lower()[:20000]
            or "/checkout/pay/" in str(form.get("action", "")).lower(),
            "review": "review your order" in html.lower()[:20000]
            or "/place-order" in str(form.get("action", "")).lower()
            or soup.select_one("[data-test-id='order-summary']") is not None,
        },
    }


def parse_order_confirmation(html: str) -> dict | None:
    """Detect an order-confirmation page; return summary if found."""
    low = html.lower()
    if not any(
        marker in low for marker in ("order placed", "your order is confirmed", "thank you")
    ):
        return None
    oid_match = _ORDER_ID_RE.search(html)
    soup = _soup(html)
    total = _text(soup.select_one(".grand-total-price, [data-test-id='grand-total']"))
    delivery = _text(soup.select_one(".delivery-date, [data-test-id='delivery-date']"))
    return {
        "placed": True,
        "order_id": oid_match.group(0) if oid_match else None,
        "total": total,
        "estimated_delivery": delivery,
    }
