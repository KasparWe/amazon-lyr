<p align="center">
  <img src="https://github.com/KasparWe/amazon-lyr/actions/workflows/ci.yml/badge.svg" alt="CI">
  <img src="https://img.shields.io/badge/python-3.11%2B-2b6cb0?style=flat&logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/tools-20-c026d3?style=flat&logo=modelcontextprotocol&logoColor=white" alt="20 MCP tools">
  <img src="https://img.shields.io/badge/license-MIT-059669?style=flat" alt="MIT">
</p>

> [!IMPORTANT]
> **Hardened fork of [ishan-parihar/amazon-lyr](https://github.com/ishan-parihar/amazon-lyr).**
> Buying, order cancellation and returns are switched off by code constants in
> `amazon_lyr/constants.py` (`ORDERING_ENABLED`, `ORDER_CANCEL_ENABLED`,
> `RETURNS_ENABLED` — all `False`). Those tools are not registered, the client
> methods refuse, and the HTTP layer refuses any checkout request or redirect.
> Search, product, reviews, cart, lists and order tracking work as normal.
> See [SECURITY_REVIEW.md](SECURITY_REVIEW.md).
>
> Install from this repo, not upstream (otherwise `amazon-lyr update` would pull
> the unhardened code), and log in with `login --interactive` (`--browser` is broken).
> Works with amazon.com, amazon.ca and other storefronts — picked from your cookies.

<h1 align="center">amazon-lyr</h1>

<p align="center">
  <strong>Amazon research and shopping as MCP tools for AI agents</strong> — research
  products and reviews, manage the cart and your lists, track orders — all through
  your own logged-in browser session. No PA-API keys. Checkout stays in your browser.
</p>

---

## An agent session, start to finish

This is one real conversation with an agent running `amazon-lyr` — no steps hidden:

```text
1. search_products("noise cancelling headphones", max_price=300, min_rating=4)
2. review_insights("B0X1234567")            # star spread + keyword themes
3. search_in_reviews("B0X1234567", query="broke")     # what actually breaks?
4. add_to_cart("B0X1234567", quantity=1)     # or add_to_wishlist(asin, list_id)
   → you check out yourself in the browser
5. get_orders()
6. track_shipment("111-2223334-5556667")
7. list_returns()                            # refund status
```

Every step is a typed MCP tool call. There is no buying step: the agent can
fill the cart, but checkout happens in your browser — see [Safety model](#safety-model).

## Why it's different

| | |
|---|---|
| **No API keys** | Speaks Amazon's own web endpoints over `curl_cffi` with Chrome TLS impersonation — the same session your browser has |
| **More than scraping** | Cart mutations, your lists (view, add, create, remove), order details and shipment tracking |
| **Agent-native errors** | CAPTCHAs, sign-out redirects and soft throttles surface as typed errors with recovery hints (`possibly_throttled`, manual-fallback URLs) — never silent garbage |

## How it works

```
browser cookies ──▶ AmazonClient (curl_cffi, captcha-aware)
                        │
                        ▼
              bs4 parsers ──▶ normalized dicts ──▶ 20 FastMCP tools
                                                   (readOnly / destructive annotations)
```

Auth is your own session. `amazon-lyr login --interactive` opens your
installed Google Chrome with a fresh temporary profile on the storefront; you
sign in yourself (password, one-time code, CAPTCHA — nothing is automated),
the session cookies for that storefront are read over Chrome's local DevTools
port, and the window closes and the temporary profile is deleted. Add
`--send-to <ssh-host>` to sign in on this machine but store the session on a
server (it is piped over ssh stdin and never saved locally). Alternatively,
`login --cookies-file` imports a JSON export (e.g. Cookie-Editor), or `-` for
stdin. Reads need the storefront's login token
(`at-main` on amazon.com, `at-acbca` on amazon.ca); cart, wishlist and orders also need the
session cookies (`session-id`, `ubid-main` / `ubid-acbca`). Browser import via the optional
`obscura` extra is broken and not recommended (see the security review).

## Install

```bash
# Persistent install — uv tool (isolated, upgrades from this repo)
uv tool install git+https://github.com/KasparWe/amazon-lyr.git

# Or clone and run the installer (also handles pipx / pip fallbacks):
git clone https://github.com/KasparWe/amazon-lyr && bash amazon-lyr/install.sh
```

Then authenticate once and serve:

```bash
amazon-lyr login --interactive --marketplace amazon.ca   # sign in in a Chrome window
# or sign in here, keep the session on a server running the MCP:
amazon-lyr login --interactive --marketplace amazon.ca --send-to myserver
amazon-lyr search "mechanical keyboard"      # shell works immediately
amazon-lyr serve                             # MCP server (stdio)
```

```json
{
  "mcpServers": {
    "amazon": {
      "command": "amazon-lyr",
      "args": ["serve"]
    }
  }
}
```

## Tools (20)

| Category | Tools |
|----------|-------|
| **Search** | `search_products` (department, price range, min-rating filters) |
| **Product** | `get_product`, `compare_products` (up to 5 ASINs) |
| **Reviews** | `get_reviews`, `review_insights`, `search_in_reviews` |
| **Cart** | `add_to_cart`, `view_cart`, `update_cart_quantity`, `remove_from_cart` |
| **Lists** | `get_lists`, `get_wishlist` (any list), `add_to_wishlist` (to any list), `create_list` (private/shared/public), `remove_from_list` |
| **Orders** | `get_orders`, `get_order_details`, `track_shipment` |
| **Returns** | `list_returns` (read-only) |
| **Session** | `get_session_status` |

## Safety model

Nothing an agent sends over MCP can move money. Product titles and reviews
are untrusted text, so a prompt injection is assumed possible; the defences
don't rely on the model behaving.

1. **Kill switches** — `ORDERING_ENABLED`, `ORDER_CANCEL_ENABLED` and
   `RETURNS_ENABLED` in `amazon_lyr/constants.py` are plain code constants,
   not env vars or tool parameters. While `False`, `checkout_preview`,
   `place_order`, `cancel_order` and `initiate_return` aren't registered and
   their client methods refuse before any request.
2. **Transport guard** — every request, and every redirect hop, must be https
   on the Amazon domain, and anything that looks like checkout, `/gp/buy/`,
   place-order, buy-now or 1-click is refused by URL path and form field name.
3. **Narrow cart writes** — `remove_from_cart` / `update_cart_quantity` only
   accept `submit.delete.<id>` / `quantityBox.<id>` fields from `view_cart`.
4. **Session gate** — every mutation requires full login cookies; read-only
   sessions are refused before any request.
5. **Local-only HTTP** — `serve --transport streamable-http` refuses to bind
   anything but a loopback address (the transport has no authentication).

`tests/test_kill_switch.py` runs against the shipped constants and fails if a
switch is flipped. Re-enabling buying needs more than flipping the flag — see
"If you want to turn ordering on later" in [SECURITY_REVIEW.md](SECURITY_REVIEW.md).

Your Amazon session cookies live in plain JSON at
`~/.config/amazon-lyr/cookies.json` (mode 600). Run `amazon-lyr logout` when
you're done, and don't give coding agents write access to the installed package.

## Limits

Honest ceilings, all surfaced as typed errors instead of wrong data:

- **CAPTCHA walls** — account-area pages are stricter than the catalog;
  re-import fresh cookies or wait it out (`CaptchaError` includes hints)
- **Soft throttles** — sub-3-second cadences get silent empty grids;
  responses carry `possibly_throttled: true` when suspected
- **Geo price variants** — some variants render prices client-side;
  search falls back to text-scanning, `get_product` stays authoritative
- **No buying, cancelling or returns** — by design in this fork; do those in
  the browser
- **Supported storefronts** — amazon.com, .ca, .co.uk, .de, .fr, .it, .es, .in,
  .co.jp, .com.au. The storefront is inferred from the login cookie
  (`at-acbca` → amazon.ca); set `AMAZON_MARKETPLACE=amazon.ca` to force one.
  Requests and cookies are scoped to that storefront's domain only

## Development

```bash
git clone https://github.com/KasparWe/amazon-lyr && cd amazon-lyr
uv sync --extra dev
uv run ruff check . && uv run pytest -q
```

Architecture notes, invariants, and the release process live in
[AGENTS.md](AGENTS.md).

## License

MIT © [Ishan Parihar](https://github.com/ishan-parihar). Hardening changes in this fork are under the same license.

## Updating

Use `amazon-lyr update --check` to check without changing the installation, or `amazon-lyr update` to update it. `amazon-lyr upgrade` is an alias. The command delegates to the manager that owns this installation (`uv`, `pipx`, or `pip`) and refuses editable, local-checkout, system, or unknown installs instead of replacing package-manager-owned files. For example: `uv tool upgrade amazon-lyr`. Updates come from wherever you installed from, so install from this repo to keep the hardening.
