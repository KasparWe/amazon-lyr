"""Client behavior tests with a FakeSession (no network)."""

import pytest

from amazon_lyr.exceptions import AmazonError, CaptchaError, SessionExpiredError
from amazon_lyr.scraping.api_client import AmazonClient


class FakeResponse:
    def __init__(self, text="", status_code=200, url="https://www.amazon.com/s?k=x"):
        self.text = text
        self.status_code = status_code
        self.url = url
        self.headers = {}

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, responses):
        self._responses = responses if isinstance(responses, list) else [responses]
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": str(url), **kwargs})
        resp = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        if isinstance(resp, Exception):
            raise resp
        return resp

    async def close(self):
        pass


def _client_with(monkeypatch, responses, cookies=None):
    monkeypatch.setattr("amazon_lyr.scraping.api_client.REQUEST_INTERVAL", 0)
    client = AmazonClient(cookies=cookies or {"at-main": "t"})
    fake = FakeSession(responses)
    monkeypatch.setattr(client, "_get_session", _async_returning(fake))
    return client, fake


def _async_returning(value):
    async def _get():
        return value

    return _get


async def test_captcha_detected(monkeypatch):
    captcha_html = "<html><title>Robot Check</title>Enter the characters you see below</html>"
    client, _ = _client_with(monkeypatch, FakeResponse(captcha_html))
    with pytest.raises(CaptchaError):
        await client.search_products("laptop")


async def test_signin_redirect_detected(monkeypatch):
    resp = FakeResponse("<html></html>", url="https://www.amazon.com/ap/signin?openid=x")
    client, _ = _client_with(monkeypatch, resp)
    with pytest.raises(SessionExpiredError):
        await client.view_cart()


async def test_503_maps_to_captcha(monkeypatch):
    class StatusError(Exception):
        def __init__(self):
            super().__init__("503")
            self.code = 503

    client, _ = _client_with(monkeypatch, StatusError())
    with pytest.raises(CaptchaError):
        await client.get_product("B0X1234567")


async def test_search_passes_query_params(monkeypatch):
    ok = FakeResponse("<html><body></body></html>")
    client, fake = _client_with(monkeypatch, ok)
    await client.search_products("mech keyboard", sort="price-asc-rank", page=2)
    call = fake.calls[0]
    params = call["params"] or {}
    assert params["k"] == "mech keyboard"
    assert params["s"] == "price-asc-rank"
    assert params["page"] == "2"


async def test_add_to_cart_uses_buy_box_tokens_and_verifies(monkeypatch):
    from conftest import buy_box_page, cart_page_with

    responses = [
        FakeResponse(buy_box_page()),
        FakeResponse("<html>ok</html>"),
        FakeResponse(cart_page_with("B0TESTASIN")),
    ]
    client, fake = _client_with(monkeypatch, responses)
    result = await client.add_to_cart("B0TESTASIN", quantity=2)
    get_dp, post, get_cart = fake.calls
    assert get_dp["url"].endswith("/dp/B0TESTASIN")
    assert post["method"] == "POST" and post["url"].endswith("/cart/add-to-cart")
    assert post["data"] == {
        "anti-csrftoken-a2z": "csrf-tok",
        "clientName": "OffersX_OfferDisplay_DetailPage",
        "items[0.base][asin]": "B0TESTASIN",
        "items[0.base][offerListingId]": "offer-123",
        "items[0.base][quantity]": "2",
        "submit.addToCart": "Add to cart",
    }
    assert not any("buy" in k.lower() for k in post["data"])
    assert result["status"] == "ok" and result["requested"]["asin"] == "B0TESTASIN"


async def test_add_to_cart_not_confirmed_when_item_missing(monkeypatch):
    from conftest import buy_box_page, cart_page_with

    responses = [
        FakeResponse(buy_box_page()),
        FakeResponse("<html>ok</html>"),
        FakeResponse(cart_page_with("B0OTHER000")),
    ]
    client, _ = _client_with(monkeypatch, responses)
    assert (await client.add_to_cart("B0TESTASIN"))["status"] == "not_confirmed"


async def test_add_to_cart_without_offer_raises(monkeypatch):
    from amazon_lyr.exceptions import ParseError

    client, fake = _client_with(monkeypatch, FakeResponse("<html>no buy box</html>"))
    with pytest.raises(ParseError, match="no 'Add to cart' offer"):
        await client.add_to_cart("B0TESTASIN")
    assert len(fake.calls) == 1


async def test_cookies_set_on_session():

    client = AmazonClient(cookies={"at-main": "abc", "ubid-main": "123"})
    session = await client._get_session()
    try:
        jar_names = {c.name: c.value for c in session.cookies.jar}
        assert jar_names["at-main"] == "abc"
        assert jar_names["ubid-main"] == "123"
    finally:
        await client.close()


# ── retry / transport / filter edge cases ─────────────────────────────────


class AlwaysFails:
    def __init__(self):
        self.calls = 0

    async def request(self, *a, **k):
        self.calls += 1
        raise ConnectionError("boom")

    async def close(self):
        pass


async def test_retries_exhausted_then_wrapped(monkeypatch):
    monkeypatch.setattr("amazon_lyr.scraping.api_client.RETRY_BACKOFF", 0)
    from amazon_lyr.exceptions import AmazonError

    fake = AlwaysFails()
    client, _ = _client_with(monkeypatch, FakeResponse(""))
    monkeypatch.setattr(client, "_get_session", _fake_session(fake))
    with pytest.raises(AmazonError, match="after 3 attempts"):
        await client.search_products("x")
    assert fake.calls == 3


def _fake_session(session):
    async def _get():
        return session

    return _get


async def test_404_raises_not_found(monkeypatch):
    from amazon_lyr.exceptions import NotFoundError

    class NotFound(Exception):
        pass

    resp = FakeResponse("<html></html>", status_code=404)
    # _check_page inspects status_code directly.
    client, _ = _client_with(monkeypatch, resp)

    orig_check = client._check_page

    def check(r):
        if r.status_code == 404:
            raise NotFoundError()
        return orig_check(r)

    monkeypatch.setattr(client, "_check_page", check)
    with pytest.raises(NotFoundError):
        await client.get_product("B0MISSING9")


async def test_signin_via_form_action_marker(monkeypatch):
    html = '<html><body><form action="/ap/signin" method="post">login</form></body></html>'
    resp = FakeResponse(html)
    client, _ = _client_with(monkeypatch, resp)
    with pytest.raises(SessionExpiredError):
        await client.view_cart()


async def test_automated_access_block_is_captcha(monkeypatch):
    html = "<p>To discuss automated access to Amazon data please contact api-services-support@amazon.com</p>"
    client, _ = _client_with(monkeypatch, FakeResponse(html))
    with pytest.raises(CaptchaError):
        await client.search_products("x")


async def test_empty_body_returns_empty_string(monkeypatch):
    client, _ = _client_with(monkeypatch, FakeResponse(""))
    assert await client.get_html("/s", params={"k": "x"}) == ""


async def test_get_product_hits_dp_path(monkeypatch):
    ok = FakeResponse("<html><body></body></html>")
    client, fake = _client_with(monkeypatch, ok)
    await client.get_product("B08N5WRWNW")
    assert "/dp/B08N5WRWNW" in fake.calls[0]["url"]


async def test_post_form_sets_content_type(monkeypatch):
    ok = FakeResponse('<div id="sc-empty-cart"></div>')
    client, fake = _client_with(monkeypatch, ok)
    await client.post_form("/gp/cart/view.html", data={"a": "b"})
    assert fake.calls[0]["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


async def test_remove_from_cart_reports_uncertain_when_item_stays(monkeypatch):
    from conftest import cart_page_with

    page = cart_page_with("B0TESTASIN")
    client, fake = _client_with(monkeypatch, [FakeResponse(page), FakeResponse(page)])
    result = await client.remove_from_cart("submit.delete-active.K1")
    assert result["status"] == "uncertain"
    post = fake.calls[1]
    assert post["url"].startswith("https://www.amazon.com/cart/ref=ord_cart_shr")
    assert post["data"] == {"anti-csrftoken-a2z": "cart-tok", "submit.delete-active.K1": "Delete"}


async def test_remove_from_cart_unknown_key(monkeypatch):
    from conftest import cart_page_with

    client, fake = _client_with(monkeypatch, FakeResponse(cart_page_with("B0TESTASIN")))
    with pytest.raises(AmazonError, match="isn't in the cart"):
        await client.remove_from_cart("submit.delete-active.NOPE")
    assert len(fake.calls) == 1


async def test_quantity_clamping_in_client(monkeypatch):
    from conftest import buy_box_page, cart_page_with

    for raw, expected in ((100, "30"), (0, "1"), (-5, "1"), (7, "7")):
        responses = [
            FakeResponse(buy_box_page("B0Q1234567")),
            FakeResponse("ok"),
            FakeResponse(cart_page_with("B0Q1234567")),
        ]
        client, fake = _client_with(monkeypatch, responses)
        await client.add_to_cart("B0Q1234567", quantity=raw)
        assert fake.calls[1]["data"]["items[0.base][quantity]"] == expected


async def test_search_filter_params_independent(monkeypatch):
    ok = FakeResponse("<html></html>")
    client, fake = _client_with(monkeypatch, ok)
    await client.search_products(
        "ssd", price_min=50, price_max=200, min_rating=4, department="electronics"
    )
    params = fake.calls[0]["params"]
    assert params["low-price"] == "50"
    assert params["high-price"] == "200"
    assert params["rh"] == "p_72:1254471011"
    assert params["i"] == "electronics"
    assert "rh" not in {k: v for k, v in params.items() if v == ""}  # no empty rh


async def test_min_rating_out_of_map_is_ignored(monkeypatch):
    ok = FakeResponse("<html></html>")
    client, fake = _client_with(monkeypatch, ok)
    await client.search_products("ssd", min_rating=5)
    assert "rh" not in fake.calls[0]["params"]


async def test_rate_limiter_enforces_interval():
    import time

    from amazon_lyr.ratelimit import RateLimiter

    limiter = RateLimiter(min_interval=0.05)
    await limiter.acquire()
    start = time.monotonic()
    await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.03  # second call must wait out the interval
