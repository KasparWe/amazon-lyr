"""Cookie import: obscura-core browser extractor (optional) with JSON fallback.

Primary path is explicit JSON (`AMAZON_COOKIES` env var or `--login --cookies-file`),
which always works. Browser extraction uses obscura-core when the optional extra
is installed.
"""

from __future__ import annotations

from amazon_lyr.logging_config import logger
from amazon_lyr.marketplace import resolve_marketplace

SUPPORTED_BROWSERS = ["chrome", "brave", "firefox", "edge", "zen"]


def import_from_browser(browser: str | None = None) -> dict[str, str]:
    """Extract Amazon cookies from an installed browser via obscura-core.

    Raises ImportError with setup instructions if obscura-core is not installed.
    """
    try:
        from obscura_core import BrowserCookieExtractor  # noqa: PLC0415
    except ImportError as e:
        raise ImportError(
            "Browser extraction needs the optional obscura extra. Install with "
            "`uv sync --extra obscura` or `pip install 'amazon-lyr[obscura]'`. "
            "Alternatively export cookies as JSON and run "
            "`amazon-lyr --login --cookies-file cookies.json`."
        ) from e

    target = browser or "chrome"
    domain = resolve_marketplace().domain
    logger.info("Extracting %s cookies from %s…", domain, target)
    cookies = BrowserCookieExtractor.extract(domain=domain, browser=target, validate=False)
    return {k: v for k, v in cookies.items() if v}


def cookies_from_json(data: object) -> dict[str, str]:
    """Accept ``{"name": "value"}`` or a cookie-editor style list of
    ``{"name", "value", "domain"}`` objects (only amazon.* cookies are kept)."""
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    if isinstance(data, list):
        out: dict[str, str] = {}
        for c in data:
            if not isinstance(c, dict) or "name" not in c or "value" not in c:
                raise ValueError("cookie list entries need 'name' and 'value'")
            domain = str(c.get("domain", "")).lstrip(".").lower()
            if domain and not domain.startswith("amazon.") and ".amazon." not in domain:
                continue
            out[str(c["name"])] = str(c["value"])
        return out
    raise ValueError("cookies file must be a JSON object or a list of cookies")
