from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv


def _serve(args) -> None:  # noqa: ANN001 - argparse Namespace
    from amazon_lyr.server import create_mcp_server

    mcp = create_mcp_server()
    transport = args.transport or os.getenv("TRANSPORT", "stdio")
    if transport == "streamable-http":
        host = args.host or os.getenv("HOST", "127.0.0.1")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            # The HTTP transport has no authentication; anyone who can reach it
            # can drive your Amazon session.
            raise SystemExit(
                f"Refusing to serve unauthenticated MCP on non-loopback host {host!r}."
            )
        port = int(args.port or os.getenv("PORT", "8000"))
        path = args.http_path or os.getenv("HTTP_PATH", "/mcp")
        mcp.run(transport="http", host=host, port=port, path=path)
    else:
        mcp.run()


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    import argparse  # noqa: PLC0415 - after load_dotenv for parity with family CLIs

    parser = argparse.ArgumentParser(
        prog="amazon-lyr",
        description="Amazon research & shopping MCP server + CLI (browser-cookie auth)",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="JSON output for data commands")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="Run the MCP server (default command)")
    p_serve.add_argument("--transport", choices=["stdio", "streamable-http"], default=None)
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--http-path", default=None)

    sub.add_parser("status", help="Show auth status", parents=[common])

    p_login = sub.add_parser("login", help="Import Amazon cookies from browser or JSON file")
    p_login.add_argument(
        "--browser", default=None, help="chrome|brave|firefox|edge|zen (needs obscura extra)"
    )
    p_login.add_argument("--cookies-file", default=None, help="Path to cookies JSON file")

    sub.add_parser("logout", help="Delete stored cookies")

    p_search = sub.add_parser("search", help="Search products", parents=[common])
    p_search.add_argument("query")
    p_search.add_argument("--department", default=None)
    p_search.add_argument("--sort", default=None)
    p_search.add_argument("--page", type=int, default=1)

    p_prod = sub.add_parser("product", help="Get product details by ASIN", parents=[common])
    p_prod.add_argument("asin")

    p_rev = sub.add_parser("reviews", help="Get reviews by ASIN", parents=[common])
    p_rev.add_argument("asin")
    p_rev.add_argument("--stars", dest="filter_by_star", default=None)
    p_rev.add_argument("--sort", dest="sort_by", default="helpful")

    sub.add_parser("cart", help="View cart", parents=[common])

    p_addcart = sub.add_parser("cart-add", help="Add an item to the cart", parents=[common])
    p_addcart.add_argument("asin")
    p_addcart.add_argument("--quantity", type=int, default=1)

    sub.add_parser("orders", help="List orders", parents=[common])

    p_order = sub.add_parser("order", help="Full details for one order", parents=[common])
    p_order.add_argument("order_id", help="e.g. 123-1234567-1234567")

    p_track = sub.add_parser("track", help="Tracking/status for one order", parents=[common])
    p_track.add_argument("order_id")

    sub.add_parser("returns", help="List returns and refunds", parents=[common])

    p_update = sub.add_parser(
        "update", aliases=["upgrade"], help="Update through the owning package manager"
    )
    p_update.add_argument("--check", action="store_true", help="Check without installing")
    p_update.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="Emit JSON status"
    )
    parser.add_argument("--json", action="store_true", help="JSON output for data commands")
    parser.add_argument("--version", action="store_true", help="Print version")

    args = parser.parse_args(argv)

    if args.command in {"update", "upgrade"}:
        from amazon_lyr.updater import run_update

        update_args = (["--check"] if args.check else []) + (["--json"] if args.json else [])
        raise SystemExit(run_update(update_args, application="amazon-lyr", package="amazon-lyr"))
    from amazon_lyr.cli_actions import run_command  # noqa: PLC0415

    asyncio.run(run_command(args))


if __name__ == "__main__":
    main()
