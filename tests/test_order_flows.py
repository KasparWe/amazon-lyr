"""Client flows for order management, returns, and review research (FakeSession)."""

import pytest
from conftest import (
    CANCEL_PAGE_HTML,
    ORDER_DETAILS_HTML,
    ORDER_ID,
    RETURN_FORM_HTML,
    FakeResponse,
    FakeSession,
    session_getter,
)

from amazon_lyr.exceptions import AmazonError, ParseError, SessionExpiredError
from amazon_lyr.scraping.api_client import AmazonClient

# Upstream money-flow tests: run with the kill switches flipped on.
pytestmark = pytest.mark.usefixtures("money_ops_enabled")


REVIEWS_PAGE_1 = """
<div data-hook="review" id="R1">
  <a data-hook="review-title"><span>Battery great</span></a>
  <span data-hook="review-body">Amazing battery life</span>
  <span data-hook="review-star-rating"><i class="a-icon-star"><span class="a-icon-alt">5.0 out of 5 stars</span></i></span>
  <span data-hook="avp-badge">Verified Purchase</span>
</div>
"""
REVIEWS_PAGE_2 = """
<div data-hook="review" id="R2">
  <a data-hook="review-title"><span>Broke fast</span></a>
  <span data-hook="review-body">Poor quality battery died in days</span>
  <span data-hook="review-star-rating"><i class="a-icon-star"><span class="a-icon-alt">1.0 out of 5 stars</span></i></span>
</div>
"""


def _client(monkeypatch, responses):
    client = AmazonClient(cookies={"at-main": "t"})
    fake = FakeSession(responses)
    monkeypatch.setattr(client, "_get_session", session_getter(fake))
    return client, fake


# ── order id validation ──────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["", "abc", "111-222-333", "111-2223334"])
async def test_order_details_rejects_malformed_ids(monkeypatch, bad):
    client, _ = _client(monkeypatch, FakeResponse("<html></html>"))
    with pytest.raises(AmazonError, match="123-1234567-1234567"):
        await client.get_order_details(bad)


# ── tracking ─────────────────────────────────────────────────────────────


async def test_track_shipment_extracts_from_details_page(monkeypatch):
    client, fake = _client(monkeypatch, [FakeResponse(ORDER_DETAILS_HTML)])
    result = await client.track_shipment(ORDER_ID)
    assert result["tracking_numbers"][0]["number"] == "TBA123456789"
    call = fake.calls[0]
    assert "order-details" in str(call["url"]) or str(call["url"]).endswith(
        "/gp/your-account/order-details"
    )
    assert (call["params"] or {}).get("orderID") == ORDER_ID
    assert result["note"] is None


async def test_track_unshipped_order_reports_note(monkeypatch):
    html = ORDER_DETAILS_HTML.replace("TBA123456789", "")
    client, _ = _client(monkeypatch, [FakeResponse(html)])
    result = await client.track_shipment(ORDER_ID)
    assert result["tracking_numbers"] is None
    assert "not have shipped" in result["note"]


# ── returns ──────────────────────────────────────────────────────────────


async def test_initiate_return_happy_path(monkeypatch):
    confirm_html = "<html><body>Your return is started. Refund summary below.</body></html>"
    client, fake = _client(
        monkeypatch, [FakeResponse(RETURN_FORM_HTML), FakeResponse(confirm_html)]
    )
    result = await client.initiate_return(ORDER_ID, asin="Cable Pack", reason="defective")
    assert result["status"] == "submitted"
    assert result["item"] == "Cable Pack"
    posted = fake.calls[1]
    assert posted["method"] == "POST"
    # Hidden tokens + chosen checkbox + mapped reason code replayed.
    assert posted["data"]["anti-csrftoken-a2z"] == "tok123"
    assert posted["data"]["itemSelector"] == "B0BBB"
    assert result["reason_submitted"] == "DEFECTIVE_ITEM"


async def test_initiate_return_unknown_asin_lists_options(monkeypatch):
    client, _ = _client(
        monkeypatch, [FakeResponse(RETURN_FORM_HTML), FakeResponse("<html></html>")]
    )
    with pytest.raises(AmazonError, match="USB-C Hub"):
        await client.initiate_return(ORDER_ID, asin="nonexistent")


async def test_initiate_return_wizard_gone_raises_parse_error(monkeypatch):
    client, _ = _client(monkeypatch, [FakeResponse("<html><body>maintenance</body></html>")])
    with pytest.raises(ParseError, match="returns/orders"):
        await client.initiate_return(ORDER_ID)


async def test_initiate_return_extra_step_reported_not_faked(monkeypatch):
    client, _ = _client(
        monkeypatch,
        [
            FakeResponse(RETURN_FORM_HTML),
            FakeResponse("<html><body>Select a reason to continue</body></html>"),
        ],
    )
    result = await client.initiate_return(ORDER_ID)
    assert result["status"] == "in_progress"
    assert "Finish at" in result["next_step"]


async def test_list_returns_empty_without_false_positives(monkeypatch):
    """Plain 'sign in' copy in a page must NOT raise — only real signin forms do
    (centralized in _check_page). Empty returns list is a valid result."""
    html = "<html><body><p>No returns to show. Someone should sign in somewhere.</p></body></html>"
    client, _ = _client(monkeypatch, [FakeResponse(html)])
    result = await client.list_returns()
    assert result["count"] == 0


async def test_list_returns_real_signin_form_detected(monkeypatch):
    html = (
        '<html><body><form action="/ap/signin" method="post">'
        '<input name="email"></form></body></html>'
    )
    client, _ = _client(monkeypatch, [FakeResponse(html)])
    with pytest.raises(SessionExpiredError):
        await client.list_returns()


# ── cancellation ─────────────────────────────────────────────────────────


async def test_cancel_order_two_step_flow(monkeypatch):
    confirmed = "<html><body>Your order has been cancelled.</body></html>"
    client, fake = _client(
        monkeypatch,
        [
            FakeResponse(ORDER_DETAILS_HTML),
            FakeResponse(CANCEL_PAGE_HTML),
            FakeResponse(confirmed),
        ],
    )
    result = await client.cancel_order(ORDER_ID)
    assert result["status"] == "cancelled"
    cancel_post = fake.calls[2]
    assert cancel_post["data"]["checkedItems"] == "line1"
    assert cancel_post["data"]["orderID"] == ORDER_ID  # hidden fields replayed
    assert cancel_post["data"]["submit.save"] == "Cancel checked items"


async def test_cancel_order_without_cancel_option(monkeypatch):
    shipped = ORDER_DETAILS_HTML.replace(
        "/gp/cancel-your-orders?orderID=111-2223334-5556667", "/help"
    )
    client, _ = _client(monkeypatch, [FakeResponse(shipped)])
    with pytest.raises(AmazonError, match="before they ship"):
        await client.cancel_order(ORDER_ID)


# ── review research ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    """Multi-page review fetching sleeps REQUEST_INTERVAL between pages."""
    monkeypatch.setattr("amazon_lyr.scraping.api_client.REQUEST_INTERVAL", 0)


def _reviews_client(monkeypatch, pages_html):
    responses = [FakeResponse(p) for p in pages_html]
    return _client(monkeypatch, responses)


async def test_review_insights_aggregates_pages(monkeypatch):
    client, fake = _reviews_client(monkeypatch, [REVIEWS_PAGE_1, REVIEWS_PAGE_2])
    ins = await client.review_insights("B0X1234567", max_pages=3)
    assert ins["total"] == 2
    assert ins["verified_percent"] == 50
    words = {k["word"] for k in ins["top_keywords"]}
    assert "battery" in words
    crit = {k["word"] for k in ins["critical_keywords"]}
    assert "poor" in crit or "died" in crit
    assert len(fake.calls) == 3  # page1, page2 (new ids), page3 empty → stop


async def test_review_insights_dedupes_repeated_ids(monkeypatch):
    client, fake = _reviews_client(monkeypatch, [REVIEWS_PAGE_1, REVIEWS_PAGE_1])
    ins = await client.review_insights("B0X1234567", max_pages=5)
    assert ins["total"] == 1  # second page had identical review id


async def test_search_in_reviews_filters_and_scores(monkeypatch):
    client, _ = _reviews_client(monkeypatch, [REVIEWS_PAGE_1, REVIEWS_PAGE_2])
    out = await client.search_in_reviews("B0X1234567", "battery")
    assert out["matches"] == 2
    assert out["of_scanned"] == 2
    assert out["average_rating_of_matches"] == 3.0


async def test_search_in_reviews_empty_query_rejected(monkeypatch):
    client, _ = _reviews_client(monkeypatch, [REVIEWS_PAGE_1])
    with pytest.raises(ValueError, match="query"):
        await client.search_in_reviews("B0X1234567", "   ")
