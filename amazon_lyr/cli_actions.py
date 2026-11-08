"""Command execution + output formatting (TOON default, JSON with --json)."""

from __future__ import annotations

import json
import re
import sys

from amazon_lyr import __version__
from amazon_lyr.authentication import get_auth_status
from amazon_lyr.cookie_import import cookies_from_json, import_from_browser
from amazon_lyr.exceptions import AmazonError
from amazon_lyr.logging_config import logger
from amazon_lyr.marketplace import get_marketplace, resolve_marketplace
from amazon_lyr.session_state import clear_cookies, save_cookies

# ── TOON helpers ──────────────────────────────────────────────────────────


_NUMERIC_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")


def _quote(val: str) -> str:
    # Quote anything that could be misread as bool/number, contains special
    # chars, or has padding — so a TOON roundtrip preserves the original type.
    if (
        val in ("true", "false", "null")
        or any(c in val for c in ':,"[]{}#\n')
        or val != val.strip()
        or _NUMERIC_RE.fullmatch(val)
    ):
        return '"' + val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return val


def _kv(key: str, val) -> str:  # noqa: ANN001
    if isinstance(val, bool):
        return f"{key}: {'true' if val else 'false'}"
    if isinstance(val, int):
        return f"{key}: {val}"
    if val is None:
        return f"{key}: null"
    return f"{key}: {_quote(str(val))}"


def _cell(v) -> str:  # noqa: ANN001 - tabular cell honors original type
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return str(v)
    return _quote(str(v))


def toon_object(fields: dict) -> str:
    lines = []
    for k, v in fields.items():
        if isinstance(v, dict):
            lines.append(f"{k}:")
            lines += [f"  {_kv(sk, sv)}" for sk, sv in v.items()]
        elif isinstance(v, list) and v and all(isinstance(i, dict) for i in v):
            keys = list(v[0].keys())
            lines.append(f"{k}[{len(v)}]{{{','.join(keys)}}}:")
            for item in v:
                lines.append("  " + ",".join(_cell(item.get(kk)) for kk in keys))
        elif isinstance(v, list):
            lines.append(f"{k}[{len(v)}]: " + ",".join(_quote(str(x)) for x in v))
        else:
            lines.append(_kv(k, v))
    return "\n".join(lines)


def axi_error(msg: str, hint: str | None = None) -> None:
    print(_kv("error", msg))
    if hint:
        print(_kv("help", hint))
    sys.exit(2)


_COMPACT = {
    "results": ["asin", "title", "price_value", "rating"],
    "reviews": ["id", "rating", "title", "date"],
}


def _compact(data: dict, max_items: int = 8) -> dict:
    out = {}
    for key, val in data.items():
        if isinstance(val, list) and len(val) > max_items:
            out[key] = val[:max_items]
        else:
            out[key] = val
    return out


# ── commands ──────────────────────────────────────────────────────────────


def _home_view() -> None:
    status = get_auth_status()
    mode = status.get("mode", "none")
    print("amazon-lyr — Amazon research & shopping MCP server")
    print(toon_object({"auth": mode, "cookies": status.get("cookies_count", 0)}))
    print()
    print("next steps:")
    if mode == "none":
        print("  amazon-lyr login --cookies-file c.json   # exported Amazon cookies")
    else:
        print('  amazon-lyr search "mechanical keyboard"')
        print('  MCP config: {"command": "amazon-lyr", "args": ["serve"]}')
    print("  amazon-lyr --help                   # full command list")


async def run_command(args) -> None:  # noqa: ANN001, C901 - flat dispatch is fine
    if args.version:
        print(__version__)
        return

    cmd = args.command

    if cmd is None:
        _home_view()
        return

    if cmd == "status":
        print(json.dumps(get_auth_status()) if args.json else toon_object(get_auth_status()))
        return

    if cmd == "logout":
        removed = clear_cookies()
        print(toon_object({"logged_out": removed}))
        return

    if cmd == "login":
        cookies: dict[str, str] = {}
        if getattr(args, "interactive", False):
            from amazon_lyr import interactive_login as il  # noqa: PLC0415

            try:
                market = (
                    get_marketplace(args.marketplace) if args.marketplace else resolve_marketplace()
                )
                il.print_instructions(market, args.send_to)
                cookies = await il.interactive_login(market, timeout=args.timeout)
            except (ValueError, AmazonError) as e:
                axi_error(str(e), hint="Retry with: amazon-lyr login --interactive")
        elif args.cookies_file:
            from pathlib import Path

            try:
                raw = (
                    sys.stdin.read()
                    if args.cookies_file == "-"
                    else Path(args.cookies_file).read_text()
                )
                cookies = cookies_from_json(json.loads(raw))
            except (OSError, json.JSONDecodeError, ValueError) as e:
                axi_error(
                    f"Could not read cookies file {args.cookies_file!r}: {e}",
                    hint='Use a cookie-editor JSON export, or an object like {"at-main": "..."}.',
                )
        else:
            try:
                cookies = import_from_browser(args.browser)
            except ImportError as e:
                axi_error(
                    str(e),
                    hint="Or use --cookies-file with a JSON export of your Amazon cookies.",
                )
        try:
            market = resolve_marketplace(cookies)
        except ValueError as e:
            axi_error(str(e), hint="e.g. AMAZON_MARKETPLACE=amazon.ca amazon-lyr login ...")
        if market.login_cookie not in cookies:
            axi_error(
                f"Imported cookies are missing {market.login_cookie} "
                f"(the {market.domain} login token).",
                hint=f"Log in to {market.domain} in your browser, export again, then retry.",
            )
        if getattr(args, "send_to", None):
            from amazon_lyr.interactive_login import send_cookies_over_ssh  # noqa: PLC0415

            try:
                print(send_cookies_over_ssh(cookies, args.send_to), end="")
            except AmazonError as e:
                axi_error(str(e), hint=f"Check `ssh {args.send_to} amazon-lyr --version` works.")
            return
        from amazon_lyr.dependencies import reset_client  # noqa: PLC0415

        reset_client()  # drop any cached client built from stale cookies
        save_cookies(cookies)
        print(toon_object(get_auth_status()))
        return

    # Data commands: reads may work logged-out; the client surfaces real auth errors.
    try:
        from amazon_lyr.dependencies import get_amazon_client

        client = get_amazon_client()
        if cmd == "search":
            data = await client.search_products(
                args.query, department=args.department, sort=args.sort, page=args.page
            )
        elif cmd == "product":
            data = await client.get_product(args.asin)
        elif cmd == "reviews":
            data = await client.get_reviews(
                args.asin, filter_by_star=args.filter_by_star, sort_by=args.sort_by
            )
        elif cmd == "cart":
            data = await client.view_cart()
        elif cmd == "cart-add":
            from amazon_lyr.authentication import validate_write_session

            if not validate_write_session():
                axi_error(
                    "Cart writes need full session cookies (login token + session-id/ubid).",
                    hint="Run `amazon-lyr login` to re-import cookies from your browser.",
                )
            data = await client.add_to_cart(args.asin, quantity=args.quantity)
        elif cmd == "orders":
            data = await client.get_orders(None)
        elif cmd == "order":
            data = await client.get_order_details(args.order_id)
        elif cmd == "track":
            data = await client.track_shipment(args.order_id)
        elif cmd == "returns":
            data = await client.list_returns()
        else:
            parser_error = f"Unknown command {cmd!r}"
            axi_error(parser_error, hint="Run `amazon-lyr --help`.")
            return

        payload = _compact(data)
        print(json.dumps(payload) if args.json else toon_object(payload))
    except Exception as e:
        logger.debug("Command failed", exc_info=True)
        axi_error(str(e))
