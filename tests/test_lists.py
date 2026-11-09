"""Amazon lists (wish lists): list, view, add, create, remove — recorded 2026 flows."""

import json

import pytest
from conftest import FakeResponse, FakeSession, session_getter

from amazon_lyr.exceptions import AmazonError, ParseError
from amazon_lyr.scraping.api_client import AmazonClient
from amazon_lyr.scraping.parsers import find_list_widget_csrf, parse_list_item_for, parse_lists

CA = "https://www.amazon.ca"
LISTS_HTML = """
<div id="atwl-popover-inner"><ul id="atwl-dd-ul">
  <li class="a-dropdown-item"><span class="a-list-item">
    <a id="atwl-link-to-list-1AAAAAAAAAAAA" class="a-link-normal" href="javascript:void(0)">
      <div class="a-fixed-left-grid"><div class="a-fixed-left-grid-inner">
        <div class="a-col-left"><img alt=""></div>
        <div class="a-col-right"><span class="a-size-base">Groceries</span>
          <span class="a-size-small">Private</span></div></div></div></a></span></li>
  <li class="a-dropdown-item"><span class="a-list-item">
    <a id="atwl-link-to-list-2BBBBBBBBBBBB"><div class="a-col-right">
      <span>Gift ideas</span><span>Public</span></div></a></span></li>
</ul></div>"""
LISTS_JSON = json.dumps({"html": LISTS_HTML, "css": ""})
DP_PAGE = """
<form id="addToCart"><input type="hidden" name="anti-csrftoken-a2z" value="cart-tok"></form>
<script>P.when('A').execute(function(){ window.P.register('atwl-ready'); });</script>
<!-- sp:csrf --><input type="hidden" name="anti-csrftoken-a2z" value="list-tok">"""
CREATE_FORM = (
    '<form><input type="hidden" name="anti-csrftoken-a2z" value="create-tok">'
    '<input id="list-name" name="list-name" value="Shopping List"></form>'
)
LIST_PAGE = """
<ul><li class="a-spacing-base g-item-sortable" data-itemid="IYPNRBJXSB869"
     data-reposition-action-params='{"itemExternalId":"ASIN:B000000009|A2EUQ1WTGCTBG2"}'>
  <input type="hidden" name="anti-csrftoken-a2z" value="item-tok">
  <a id="itemName_IYPNRBJXSB869" title="Gooseneck Kettle" href="/dp/B000000009/?coliid=I1">Gooseneck Kettle</a>
</li></ul>"""
EMPTY_LIST = "<ul></ul>"


def _client(monkeypatch, responses):
    monkeypatch.setattr("amazon_lyr.scraping.api_client.REQUEST_INTERVAL", 0)
    client = AmazonClient(
        cookies={"at-acbca": "t", "session-id": "140-1234567-7654321"}, marketplace="amazon.ca"
    )
    fake = FakeSession([FakeResponse(r, url=CA + "/x") for r in responses])
    monkeypatch.setattr(client, "_get_session", session_getter(fake))
    return client, fake


def test_parse_lists_names_ids_privacy():
    assert parse_lists(LISTS_HTML)["lists"] == [
        {"list_id": "1AAAAAAAAAAAA", "name": "Groceries", "privacy": "private"},
        {"list_id": "2BBBBBBBBBBBB", "name": "Gift ideas", "privacy": "public"},
    ]


def test_list_widget_token_is_not_the_cart_token():
    assert find_list_widget_csrf(DP_PAGE) == "list-tok"
    assert find_list_widget_csrf('<input name="anti-csrftoken-a2z" value="x">') is None


def test_parse_list_item_for():
    item = parse_list_item_for(LIST_PAGE, "B000000009")
    assert item == {
        "item_id": "IYPNRBJXSB869",
        "item_external_id": "ASIN:B000000009|A2EUQ1WTGCTBG2",
        "title": "Gooseneck Kettle",
        "csrf": "item-tok",
    }
    assert parse_list_item_for(LIST_PAGE, "B000000001") is None


async def test_get_lists_reads_atlaapi_json(monkeypatch):
    client, fake = _client(monkeypatch, [LISTS_JSON])
    out = await client.get_lists()
    assert out["count"] == 2
    assert fake.calls[0]["url"] == CA + "/hz/wishlist/addtolist/atlaapi"


async def test_add_to_list_posts_recorded_shape(monkeypatch):
    client, fake = _client(monkeypatch, [DP_PAGE, "<div>1 item added to Groceries</div>"])
    out = await client.add_to_list("B000000009", "1AAAAAAAAAAAA")
    assert out == {"status": "ok", "asin": "B000000009", "list_id": "1AAAAAAAAAAAA"}
    post = fake.calls[1]
    assert post["url"] == CA + "/hz/wishlist/additemtolist"
    assert post["headers"]["anti-csrftoken-a2z"] == "list-tok"
    assert post["headers"]["X-Requested-With"] == "XMLHttpRequest"
    assert post["data"] == {
        "asin": "B000000009",
        "vendorId": "website.wishlist.detail.add",
        "listExternalId": "1AAAAAAAAAAAA",
        "listType": "wishlist",
        "isAjax": "1",
    }


async def test_add_to_list_defaults_to_first_list_and_reports_duplicates(monkeypatch):
    client, fake = _client(monkeypatch, [LISTS_JSON, DP_PAGE, "This item was already in Groceries"])
    out = await client.add_to_list("B000000009")
    assert out["status"] == "already_in_list" and out["list_id"] == "1AAAAAAAAAAAA"


@pytest.mark.parametrize("bad", ["../x", "abc", "1AAAAAAAAAAAA/../../gp/buy", "A" * 30])
async def test_list_ids_are_validated(monkeypatch, bad):
    client, fake = _client(monkeypatch, [])
    with pytest.raises(AmazonError, match="not an Amazon list id"):
        await client.add_to_list("B000000009", bad)
    assert fake.calls == []


async def test_create_list_with_first_item(monkeypatch):
    after = LISTS_JSON.replace("Gift ideas", "Camping")
    client, fake = _client(monkeypatch, [CREATE_FORM, '{"success":true}', after])
    out = await client.create_list("  Camping ", privacy="public", asin="B000000009")
    assert out["status"] == "ok" and out["list"]["list_id"] == "2BBBBBBBBBBBB"
    post = fake.calls[1]
    assert post["url"] == CA + "/hz/wishlist/create/newlist"
    assert post["headers"]["anti-csrftoken-a2z"] == "create-tok"
    assert post["data"] == {
        "asin": "B000000009",
        "listName": "Camping",
        "sid": "140-1234567-7654321",
        "vendorId": "website.wishlist.detail.add",
        "privacyStatus": "PUBLIC",
        "listType": "WishList",
        "isJson": "true",
    }


@pytest.mark.parametrize("name,privacy", [("", "private"), ("x" * 51, "private"), ("ok", "secret")])
async def test_create_list_validates_input(monkeypatch, name, privacy):
    client, fake = _client(monkeypatch, [])
    with pytest.raises(AmazonError):
        await client.create_list(name, privacy=privacy)
    assert fake.calls == []


async def test_create_list_unconfirmed_raises(monkeypatch):
    client, _ = _client(monkeypatch, [CREATE_FORM, "{}", LISTS_JSON])
    with pytest.raises(ParseError, match="didn't confirm"):
        await client.create_list("Nope")


async def test_remove_from_list_posts_recorded_shape(monkeypatch):
    client, fake = _client(monkeypatch, [LIST_PAGE, "{}", EMPTY_LIST])
    out = await client.remove_from_list("1AAAAAAAAAAAA", "B000000009")
    assert out["status"] == "ok"
    post = fake.calls[1]
    assert post["url"] == CA + "/hz/wishlist/removeitem"
    assert post["headers"]["anti-csrftoken-a2z"] == "item-tok"
    assert post["data"]["itemId"] == "IYPNRBJXSB869"
    assert post["data"]["itemExternalId"] == "ASIN:B000000009|A2EUQ1WTGCTBG2"
    assert post["data"]["listExternalId"] == "1AAAAAAAAAAAA"


async def test_remove_from_list_missing_item(monkeypatch):
    client, fake = _client(monkeypatch, [EMPTY_LIST])
    with pytest.raises(AmazonError, match="isn't on list"):
        await client.remove_from_list("1AAAAAAAAAAAA", "B000000009")
    assert len(fake.calls) == 1


async def test_get_wishlist_by_list_id_path(monkeypatch):
    client, fake = _client(monkeypatch, [LIST_PAGE])
    out = await client.get_wishlist("1aaaaaaaaaaaa")
    assert fake.calls[0]["url"] == CA + "/hz/wishlist/ls/1AAAAAAAAAAAA"
    assert out["items"][0]["asin"] == "B000000009"
