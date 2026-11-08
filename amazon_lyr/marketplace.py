"""Amazon marketplace selection: which storefront, cookie names and cookie domain.

Each storefront names its login cookies with its own suffix (amazon.com uses
``at-main``/``ubid-main``, amazon.ca uses ``at-acbca``/``ubid-acbca``, ...).
The marketplace comes from ``AMAZON_MARKETPLACE`` when set, otherwise it is
inferred from which login cookie the imported cookies contain.

Only hosts in ``MARKETPLACES`` are accepted, so a stray ``.env`` can't point the
client (and the transport guard's domain allowlist) at a non-Amazon host.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

# Storefront host → cookie suffix.
MARKETPLACES: dict[str, str] = {
    "www.amazon.com": "main",
    "www.amazon.ca": "acbca",
    "www.amazon.co.uk": "acbuk",
    "www.amazon.de": "acbde",
    "www.amazon.fr": "acbfr",
    "www.amazon.it": "acbit",
    "www.amazon.es": "acbes",
    "www.amazon.in": "acbin",
    "www.amazon.co.jp": "acbjp",
    "www.amazon.com.au": "acbau",
}
DEFAULT_HOST = "www.amazon.com"


@dataclass(frozen=True)
class Marketplace:
    host: str
    suffix: str

    @property
    def base_url(self) -> str:
        return f"https://{self.host}"

    @property
    def domain(self) -> str:
        """Registrable domain, e.g. amazon.ca (cookie scope and guard allowlist)."""
        return self.host.removeprefix("www.")

    @property
    def login_cookie(self) -> str:
        return f"at-{self.suffix}"

    @property
    def write_cookies(self) -> tuple[str, str]:
        return (f"ubid-{self.suffix}", "session-id")


def get_marketplace(value: str) -> Marketplace:
    """Parse ``amazon.ca``, ``www.amazon.ca`` or ``https://www.amazon.ca/`` strictly."""
    raw = value.strip()
    parts = urlsplit(raw if "://" in raw else f"https://{raw}")
    host = (parts.hostname or "").lower()
    if not host.startswith("www."):
        host = f"www.{host}"
    if parts.scheme != "https" or host not in MARKETPLACES or parts.path.strip("/"):
        supported = ", ".join(h.removeprefix("www.") for h in MARKETPLACES)
        raise ValueError(f"Unsupported Amazon marketplace {value!r}. Supported: {supported}.")
    return Marketplace(host, MARKETPLACES[host])


def resolve_marketplace(cookies: dict[str, str] | None = None) -> Marketplace:
    """AMAZON_MARKETPLACE if set, else inferred from the login cookie, else amazon.com."""
    env = os.getenv("AMAZON_MARKETPLACE", "").strip()
    if env:
        return get_marketplace(env)
    found = [h for h, s in MARKETPLACES.items() if f"at-{s}" in (cookies or {})]
    if len(found) > 1:
        names = ", ".join(h.removeprefix("www.") for h in found)
        raise ValueError(
            f"Cookies contain logins for several marketplaces ({names}). "
            "Set AMAZON_MARKETPLACE to pick one."
        )
    host = found[0] if found else DEFAULT_HOST
    return Marketplace(host, MARKETPLACES[host])
