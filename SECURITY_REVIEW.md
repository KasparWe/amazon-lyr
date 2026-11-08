# Security review: amazon-lyr

Reviewed upstream `ishan-parihar/amazon-lyr` at commit `02abe30` (2026-11-05), v0.3.0, about 2,500 lines of Python in the `amazon_lyr` package. Also checked the optional dependency `ishan-parihar/obscura-core`. Review date: 2026-11-08.

## Summary

The package only talks to `www.amazon.com`. I found no telemetry, no third-party network calls and no obfuscated code. Session cookies are stored with `chmod 600`.

The main problem is how it buys things. The `confirm="PLACE_ORDER"` "consent gate" is a value the AI fills in itself, and the tool schema even tells the AI what that value is. A second, quieter problem is worse: `checkout_preview` is labelled read-only, but it can place an order by accident.

Both issues are fixed in the `security-hardening` branch. Buying, cancelling and returns are now turned off by code constants that default to `False`.

## Findings

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| H1 | High | **`place_order` has no real consent.** The AI supplies the confirm literal. It also appears as `Literal["PLACE_ORDER"]` in the tool schema and in the server instructions. Product titles, descriptions and reviews are untrusted third-party text that gets fed straight to the model, so a prompt injection could lead to `add_to_cart` followed by `place_order`. There's no price cap, and nothing ties the preview the user saw to the order that actually gets placed (the cart can change in between). | **Fixed:** turned off by constant |
| H2 | High | **`checkout_preview` can place an order.** It's labelled `readOnlyHint: True` and needs no confirmation. It steps through checkout by clicking the *first submit button* on each page until a text check thinks it has reached the review page. Amazon's single-page checkout shows "Place your order" on the first page, so if that check misses, the preview clicks it. The "unexpected confirmation → abort" check only runs *after* the order is placed. | **Fixed:** the preview never clicks a buy/place-order button, the read-only label is corrected, and it's turned off by default |
| M1 | Medium | **`remove_from_cart` and `update_cart_quantity` accept any form field name and POST it to the cart page.** For example, `item_key="submit.checkout"` starts checkout. | **Fixed:** only `submit.delete.<id>` and `quantityBox.<id>` are accepted |
| M2 | Medium | **`cancel_order` and `initiate_return` change orders and refunds with no confirmation at all.** | **Fixed:** turned off by constants |
| M3 | Medium | **Request URLs aren't restricted.** The client follows absolute URLs scraped from pages (the cancel link, form actions), including `http://`. Cookies are scoped to `.amazon.com` but aren't marked Secure, so a plain-http URL would send the session in cleartext. | **Fixed:** https-only plus an Amazon-domain allowlist, checked on every redirect hop (redirects are followed by hand, not by curl) |
| M4 | Medium | **The `streamable-http` transport has no authentication.** `--host`, the `HOST` env var or a `.env` file can bind it to `0.0.0.0`, and then anyone on the network could drive your Amazon session. | **Fixed:** the server refuses anything other than a loopback address |
| M5 | Medium | **The full Amazon session is stored as plaintext JSON** at `~/.config/amazon-lyr/cookies.json` (mode 600). Any process running as your user can read it, and it gives access to orders, addresses and saved payment methods (not card numbers). | Not fixable in code. Run `amazon-lyr logout` when you're not using it, and keep it off your work laptop. |
| L1 | Low | **Supply chain risk from the optional `obscura` extra.** It installs the author's `obscura-core` from git `main` with no pinned version. That package reads your browser's cookie database, and one of its fallback paths runs `uv run --with browser-cookie3`, which pulls the latest version from PyPI at runtime. Separately, amazon-lyr calls `BrowserCookieExtractor.extract(domain=…, browser=…, validate=…)`, which doesn't match obscura-core's API, so **`amazon-lyr login --browser chrome` (the README's main login path) doesn't work.** | Don't install the extra; use `login --cookies-file` |
| L2 | Low | **`install.sh` is a `curl \| bash` of upstream `main`,** and it bootstraps uv with another `curl \| sh`. | Install from your private repo instead |
| L3 | Low | **`amazon-lyr update` reinstalls from wherever the package was originally installed from.** If you installed from upstream, an update would silently remove these fixes. | Install from your private repo |
| L4 | Low | **`load_dotenv()` reads `.env` from whatever directory the server starts in.** That file can set `AMAZON_COOKIES`, `AMAZON_MARKETPLACE` and `HOST`. | Accepted; M3 and M4 limit the damage |
| L5 | Low | **`scripts/genome` is an 8,000-line vendored doc tool** that runs shell commands listed in `genome.toml`. It isn't part of the installed package and CI doesn't run it. | Safe to delete |
| L6 | Low | **Order-confirmation detection is loose:** any page containing "thank you" counts as a confirmation. | Moot while ordering is off |
| I1 | Info | **amazon.com only.** The cookie names (`at-main`, `ubid-main`), the cookie domain `.amazon.com` and the cookie-import domain are hard-coded. Setting `AMAZON_MARKETPLACE=https://www.amazon.ca` won't authenticate, because amazon.ca uses `at-acbca` and `ubid-acbca`. | **Fixed:** `amazon_lyr/marketplace.py` maps an allowlist of storefronts to their cookie names; cookies, requests and the guard are scoped to the chosen storefront's domain, and `AMAZON_MARKETPLACE` must be one of them (closes the L4 `.env` redirect angle) |
| I2 | Info | **Project maturity:** one maintainer, 27 commits, about 3 weeks old. Upstream CI would fail its own `ruff format --check`. | Formatting fixed |

## What the kill switch does

The switches live in `amazon_lyr/constants.py`:

```python
ORDERING_ENABLED: Final[bool] = False  # checkout_preview + place_order
ORDER_CANCEL_ENABLED: Final[bool] = False  # cancel_order
RETURNS_ENABLED: Final[bool] = False  # initiate_return
```

These are plain code constants. They are not environment variables, config entries or tool parameters, so nothing sent over MCP can switch them on. While they're `False`, three independent layers block buying:

1. **Tool registration.** The tools aren't registered at all. The AI can't see `place_order` in `list_tools`, and calling it returns "unknown tool". The server instructions tell the AI that buying is turned off and to send you to the browser.
2. **Client methods.** `place_order`, `checkout_preview`, `_initiate_checkout`, `cancel_order` and `initiate_return` raise `OrderingDisabledError` before any HTTP request is made.
3. **Transport guard (`_guard_request`).** Every request, and every redirect hop, passes through this guard, whatever code path built it. It refuses any URL path matching checkout, `/gp/buy/`, place-order, buy-now, `handle-buy-box`, turbo or 1-click, and any POST form field name matching the same patterns. It also enforces https and the Amazon domain.

`tests/test_kill_switch.py` (31 tests) checks all three layers against the constants as shipped. If someone flips a switch, `test_shipped_constants_are_off` fails, so the change has to be made on purpose. I also confirmed the tests catch regressions: removing the preview button guard, or setting `ORDERING_ENABLED = True`, makes them fail. The upstream checkout, cancel and return tests still run with the switches turned on through a test-only fixture, so that code keeps working if you enable it later. The full suite passes: 204 tests.

**Remaining risk:** the constants protect against anything that arrives *over MCP*, including prompt injection. They can't stop an AI that has shell or file-write access on your Mac (for example a coding agent) from editing the installed `constants.py`. Don't give such an agent write access to `~/.local/share/uv/tools/amazon-lyr`.

## If you want to turn ordering on later

The current checks aren't enough. Before flipping `ORDERING_ENABLED`, add these:

1. **Human confirmation outside the model.** Use MCP elicitation (`ctx.elicit`) so the MCP client asks *you* directly and the AI can't answer on your behalf. Refuse to place the order if the client doesn't support elicitation.
2. **A spending cap.** Add a `MAX_ORDER_TOTAL` code constant and refuse any order whose parsed review-page total is missing or above the cap.
3. **Bind the preview to the order.** `checkout_preview` returns a short-lived token (an HMAC of cart ASINs, quantities and total). `place_order` requires that token and re-checks the cart before submitting.
4. **Exact button match.** Only ever submit the review page's place-order button, never "the first submit button."

## Follow-up review (2026-11-08)

A second pass over the hardening commit found one gap, now fixed:

- **Redirects bypassed the transport guard.** `_request` used curl's automatic
  redirect following, so only the first URL was checked. A response redirecting
  into checkout, to `http://` (leaking the non-Secure session cookies) or to
  another host would have been followed. Redirects are now followed by hand
  (`_send_following_redirects`), each hop goes through `_guard_request`, and
  301/302/303 drop the request body like a browser. Guard refusals are also no
  longer retried and re-wrapped as generic errors. Covered by new tests in
  `tests/test_kill_switch.py`.
