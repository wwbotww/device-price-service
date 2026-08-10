from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit


class UrlPolicyError(ValueError):
    """Raised when a URL is outside the official-channel allowlist."""


class UrlPolicy:
    def __init__(self, allowed_domains: Iterable[str]) -> None:
        self.allowed_domains = frozenset(self._normalize_domain(item) for item in allowed_domains)
        if not self.allowed_domains:
            raise ValueError("allowed_domains cannot be empty")

    def validate(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme.lower() != "https":
            raise UrlPolicyError("only HTTPS URLs are allowed")
        if parts.username is not None or parts.password is not None:
            raise UrlPolicyError("URLs containing credentials are not allowed")
        if parts.hostname is None:
            raise UrlPolicyError("URL must contain a hostname")
        host = self._normalize_domain(parts.hostname)
        if host not in self.allowed_domains:
            raise UrlPolicyError(f"domain is not allowed: {host}")
        if parts.port not in (None, 443):
            raise UrlPolicyError("only the default HTTPS port is allowed")
        return url

    @staticmethod
    def _normalize_domain(domain: str) -> str:
        normalized = domain.strip().rstrip(".").lower()
        if not normalized:
            raise ValueError("domain cannot be blank")
        return normalized.encode("idna").decode("ascii")
