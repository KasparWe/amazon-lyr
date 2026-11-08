"""Session validation against stored cookies."""

from __future__ import annotations

from amazon_lyr.marketplace import resolve_marketplace
from amazon_lyr.session_state import load_cookies


def _status(cookies: dict[str, str]) -> tuple[bool, bool]:
    """(has login token, has session context) for the resolved marketplace."""
    if not cookies:
        return False, False
    m = resolve_marketplace(cookies)
    return m.login_cookie in cookies, any(c in cookies for c in m.write_cookies)


def validate_session() -> bool:
    """True if the marketplace's login cookie (at-main, at-acbca, ...) is present."""
    return _status(load_cookies())[0]


def validate_write_session() -> bool:
    """Writes (cart/wishlist) need the login cookie plus session/ubid context cookies."""
    has_login, has_context = _status(load_cookies())
    return has_login and has_context


def get_auth_status() -> dict:
    cookies = load_cookies()
    if not cookies:
        return {"authenticated": False, "mode": "none", "cookies_count": 0}
    m = resolve_marketplace(cookies)
    has_login, has_context = _status(cookies)
    mode = "full" if has_login and has_context else "reads_only" if has_login else "degraded"
    return {
        "authenticated": has_login,
        "mode": mode,
        "marketplace": m.domain,
        "cookies_count": len(cookies),
        "has_login_token": has_login,
        "has_session_id": "session-id" in cookies,
        "has_ubid": m.write_cookies[0] in cookies,
    }
