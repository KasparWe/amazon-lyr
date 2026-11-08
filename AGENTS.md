# AGENTS.md — Agent Developer Guide for amazon-lyr

## Project Overview

- **Project**: amazon-lyr — Amazon research & shopping MCP server + CLI (search, product details, reviews, cart, orders)
- **Language**: Python 3.11+
- **Package Manager**: uv
- **Repository**: https://github.com/KasparWe/amazon-lyr (hardened fork of
  https://github.com/ishan-parihar/amazon-lyr — ordering/cancel/returns disabled)
- **Family**: part of the `-lyr` series (reddit-lyr, twitter-lyr, threads-lyr…) — browser-cookie auth, no official API keys

## Build, Lint, and Test Commands

```bash
uv sync --extra dev          # install deps
uv run ruff check .          # lint
uv run ruff format --check . # format check
uv run pytest -q             # tests (no live Amazon calls; fixtures only)
uv run amazon-lyr            # CLI home view
uv run amazon-lyr serve      # MCP server (stdio)
```

Run one test: `uv run pytest tests/test_parsers.py::test_parse_search -v`

*(Commands reviewed 2026-10-19: the section above still holds the commands that build and test this repo.)*
## Code Style

- Line length 100; ruff rules E/F/I/B/UP (see pyproject)
- `from __future__ import annotations` at top of all modules
- snake_case functions, PascalCase classes, UPPER_SNAKE constants
- Custom exceptions in `exceptions.py`, base `AmazonError(RuntimeError)`
- Tools wrap client calls in `try/except → handle_api_error(e)` (returns NoReturn)
- Read tools annotated `{"readOnlyHint": True}`; destructive ones set `destructiveHint`

## Architecture

```
amazon_lyr/
├── cli_main.py        # argparse entry; subcommands; TOON default output
├── cli_actions.py     # command dispatch + TOON/JSON formatting
├── server.py          # create_mcp_server() — FastMCP assembly
├── dependencies.py    # singleton AmazonClient accessor
├── authentication.py  # validate_session / get_auth_status
├── cookie_import.py   # obscura-core extractor (optional extra) / JSON file
├── session_state.py   # cookie persistence (~/.config/amazon-lyr/cookies.json)
├── constants.py       # endpoints, captcha markers, retry policy, kill switches
├── marketplace.py     # storefront allowlist → base URL, cookie names/domain
├── exceptions.py, error_handler.py, ratelimit.py, logging_config.py
├── scraping/
│   ├── api_client.py  # curl_cffi chrome impersonation; captcha/signin detection;
│   │                  #   ASIN/order-id validation; return/cancel wizards
│   └── parsers.py     # HTML → normalized dicts (bs4, defensive) + review insights
└── tools/
    ├── search.py, product.py               # reads (readOnlyHint=True)
    ├── reviews.py                           # get_reviews / insights / keyword scan
    ├── shopping.py                          # cart (incl. qty updates)/wishlist/get_orders
    ├── order_mgmt.py                        # details/tracking/returns/cancellation
    └── session.py                           # auth status tool
```

Key invariants:

- `_check_page()` must raise `CaptchaError` on bot-wall markers and
  `SessionExpiredError` on `/ap/signin` (full-text form-action scan) — never
  parse those pages. Sign-out detection is centralized there only.
- Parsers are defensive: missing selectors yield None/[], never raise.
- All client entry points validate identifiers (`_validate_asin`,
  order-id regex) with format hints in the error.
- Mutations (`cart-add`, cart qty/remove, returns, cancel) gate on
  `validate_write_session()` in BOTH the MCP tool layer and the CLI.
- Money-moving tools (`checkout_preview`, `place_order`, `initiate_return`,
  `cancel_order`) are MCP-only by design, and in this fork are off behind the
  code constants `ORDERING_ENABLED` / `ORDER_CANCEL_ENABLED` / `RETURNS_ENABLED`
  (all `False`): not registered, client methods raise `OrderingDisabledError`.
  Never turn these into env vars, config or tool parameters, and don't flip
  them without the steps in SECURITY_REVIEW.md.
- Every request and every redirect hop goes through `_guard_request`
  (https + Amazon domain; checkout paths/fields refused while ordering is off).
  Redirects are followed by hand — never pass `allow_redirects=True`.
- `tests/test_kill_switch.py` runs against the shipped constants; keep it green.
- Never hard-code amazon.com URLs or `at-main`: use the client's `_base` /
  `resolve_marketplace()`. New storefronts go in `MARKETPLACES` only.
- Tests never hit live Amazon — fixture HTML strings or FakeSession.

## Release Process

No CI release. Installs are source-based via `install.sh`; **pushing to main is the release**.

```bash
git checkout main && git pull
uv version --bump patch
uv run ruff check . && uv run pytest -q
git commit -m "chore(release): vX.Y.Z" && git push origin main
```

Conventional commits: `type(scope): subject` (<50 chars, imperative).
PRs: branch `feature/<short-desc>`, include model attribution, no squash.
