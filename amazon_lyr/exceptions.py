"""Exception hierarchy for amazon-lyr."""

from __future__ import annotations


class AmazonError(RuntimeError):
    """Base error for all amazon-lyr failures."""


class AuthenticationError(AmazonError):
    """Cookies are missing or invalid."""


class SessionExpiredError(AuthenticationError):
    """Amazon redirected us to /ap/signin — re-import fresh cookies."""


class CaptchaError(AmazonError):
    """Amazon served a CAPTCHA / bot-check page."""


class RateLimitError(AmazonError):
    """Retries exhausted or throttled by the local limiter."""


class NotFoundError(AmazonError):
    """ASIN / page not found (404)."""


class ParseError(AmazonError):
    """Page structure changed and parsing failed."""


class OrderingDisabledError(AmazonError):
    """A money-moving action was attempted while its kill switch is off."""
