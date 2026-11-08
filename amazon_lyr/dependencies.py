"""Shared client accessor for tools. One client per process; cookies loaded lazily."""

from __future__ import annotations

from amazon_lyr.exceptions import AuthenticationError
from amazon_lyr.marketplace import resolve_marketplace
from amazon_lyr.scraping.api_client import AmazonClient
from amazon_lyr.session_state import load_cookies

_client: AmazonClient | None = None


def get_amazon_client() -> AmazonClient:
    global _client
    if _client is None:
        cookies = load_cookies()
        marketplace = resolve_marketplace(cookies)
        if marketplace.login_cookie not in cookies:
            raise AuthenticationError(
                f"No {marketplace.domain} login cookie ({marketplace.login_cookie}) found. "
                "Run `amazon-lyr login --cookies-file` first (or set the AMAZON_COOKIES "
                "env var with your session JSON)."
            )
        _client = AmazonClient(cookies=cookies, marketplace=marketplace.base_url)
    return _client


def reset_client() -> None:
    """Drop the cached client (after cookie re-import)."""
    global _client
    _client = None
