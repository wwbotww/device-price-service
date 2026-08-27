from __future__ import annotations

from abc import ABC, abstractmethod

from device_price_service.domain.catalog_crawl import (
    DiscoveredCatalogListing,
    NormalizedListingIdentity,
    ParsedCatalogListing,
)


class CategoryRule(ABC):
    """Normalize only category-specific identity and measurement semantics."""

    profile_code: str
    version: str

    @abstractmethod
    def normalize(
        self,
        item: DiscoveredCatalogListing,
        parsed: ParsedCatalogListing,
    ) -> NormalizedListingIdentity:
        """Return comparable identity fields without applying platform rules."""


class CategoryRuleRegistry:
    def __init__(self) -> None:
        self._rules: dict[tuple[str, str], CategoryRule] = {}

    def register(self, rule: CategoryRule) -> None:
        key = self._key(rule.profile_code, rule.version)
        if key in self._rules:
            raise ValueError(f"category rule already registered: {key[0]}@{key[1]}")
        self._rules[key] = rule

    def get(self, profile_code: str, version: str) -> CategoryRule:
        key = self._key(profile_code, version)
        try:
            return self._rules[key]
        except KeyError as error:
            raise ValueError(f"category rule is not registered: {key[0]}@{key[1]}") from error

    @staticmethod
    def _key(profile_code: str, version: str) -> tuple[str, str]:
        code = profile_code.strip().lower()
        normalized_version = version.strip()
        if not code or not normalized_version:
            raise ValueError("category rule profile and version cannot be blank")
        return code, normalized_version
