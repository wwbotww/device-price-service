from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlsplit

from device_price_service.crawlers.base import AdapterContext
from device_price_service.crawlers.catalog import CatalogConnector
from device_price_service.crawlers.html import HtmlNode, parse_html
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    CatalogRegion,
    DiscoveredCatalogListing,
    DiscoveredCatalogProduct,
    ParsedCatalogListing,
    ParsedCatalogProduct,
    ParsedCatalogRow,
    SourceMerchant,
    SourcePriceCandidate,
)
from device_price_service.domain.catalog_enums import (
    Availability,
    CollectionFetchMethod,
    FeeStatus,
    OriginalPriceType,
    PriceNature,
    PriceType,
    PricingBasis,
    RegionScope,
    SellerType,
    VerificationStatus,
)
from device_price_service.domain.crawl import FetchResult
from device_price_service.domain.price_policy import PriceCandidate, PricePolicy
from device_price_service.normalization.devices import (
    DeviceSpecification,
    device_listing_key,
)
from device_price_service.normalization.specs import normalize_text

APPLE_SHOP_ORIGIN = "https://www.apple.com.cn"
APPLE_DISCOVERY_PAGES = (
    ("PHONE", f"{APPLE_SHOP_ORIGIN}/shop/buy-iphone"),
    ("TABLET", f"{APPLE_SHOP_ORIGIN}/shop/buy-ipad"),
    ("COMPUTER", f"{APPLE_SHOP_ORIGIN}/shop/buy-mac"),
    ("WATCH", f"{APPLE_SHOP_ORIGIN}/shop/buy-watch"),
)


class AppleParseError(ValueError):
    """Raised when an Apple shop fixture no longer exposes required SKU fields."""


class AppleCatalogConnector(CatalogConnector):
    product_not_found_is_definitive = True
    brand_code = "APPLE"
    channel_code = "APPLE_CN_WEB"
    connector_code = "apple-cn"
    version = "apple-cn-catalog-product"
    fetch_method = CollectionFetchMethod.HTTP
    allowed_domains = ("www.apple.com.cn",)
    default_category_codes = ("PHONE", "TABLET", "LAPTOP", "DESKTOP", "WATCH")

    def __init__(self, *, discovery_pages: tuple[tuple[str, str], ...] = APPLE_DISCOVERY_PAGES):
        self.discovery_pages = discovery_pages
        self.price_policy = PricePolicy()

    async def discover_products(
        self, context: AdapterContext, request: CatalogCollectionRequest
    ) -> list[DiscoveredCatalogProduct]:
        if request.region_scope is not RegionScope.NATIONAL or request.region_code != "CN":
            raise AppleParseError("Apple official prices require NATIONAL/CN scope")
        if request.source_item_codes:
            raise AppleParseError("Apple product discovery does not accept commodity selections")
        categories = set(request.category_codes or self.default_category_codes)
        if not categories.issubset(self.default_category_codes):
            raise AppleParseError("Apple request includes an unsupported device category")
        discovered: dict[str, DiscoveredCatalogProduct] = {}
        for category_code, page_url in self.discovery_pages:
            page_categories = (
                {"LAPTOP", "DESKTOP"} if category_code == "COMPUTER" else {category_code}
            )
            if not categories.intersection(page_categories):
                continue
            self._require_shop_path(page_url)
            result = await context.http.fetch(page_url, allowed_domains=context.allowed_domains)
            self._require_same_path(page_url, result.request_url)
            self._require_same_path(page_url, result.final_url)
            if result.status_code < 200 or result.status_code >= 300:
                raise AppleParseError(f"Apple discovery returned HTTP {result.status_code}")
            for item in self.parse_discovery(result.body, page_url, category_code):
                if item.category_code in categories:
                    discovered.setdefault(item.external_product_id, item)
        return list(discovered.values())

    async def fetch_product(
        self,
        context: AdapterContext,
        item: DiscoveredCatalogProduct,
    ) -> FetchResult:
        self._validate_product(item)
        return await context.http.fetch(item.url, allowed_domains=context.allowed_domains)

    def parse_product(
        self, item: DiscoveredCatalogProduct, result: FetchResult
    ) -> ParsedCatalogProduct:
        self._validate_product(item)
        self._require_same_path(item.url, result.request_url)
        self._require_same_path(item.url, result.final_url)
        if not 200 <= result.status_code < 300:
            raise AppleParseError(f"Apple product returned HTTP {result.status_code}")
        root = parse_html(result.body)
        heading = root.first(lambda node: node.tag == "h1")
        if heading is None:
            raise AppleParseError("Apple product page has no h1 product name")
        name = re.sub(r"^购买\s*", "", normalize_text(heading.text()))
        skus: list[dict[str, object]] = []
        seen_parts: set[str] = set()

        for anchor in root.find_all(lambda node: node.tag == "a" and bool(node.attrs.get("href"))):
            current = anchor.first(lambda node: node.has_class("current_price"))
            dimensions = self._dimensions(anchor)
            href = urljoin(result.final_url, anchor.attrs["href"])
            part_number = self._part_number(href, result.final_url)
            if current is None or not dimensions:
                if part_number is not None and (current is not None or dimensions):
                    raise AppleParseError(
                        "Apple SKU is missing explicit dimensions or current price"
                    )
                continue
            if part_number is None:
                raise AppleParseError("Apple priced SKU URL is outside the discovered product")
            if part_number in seen_parts:
                raise AppleParseError(f"Apple product repeats SKU: {part_number}")
            seen_parts.add(part_number)

            original = anchor.first(
                lambda node: (
                    any(
                        node.has_class(class_name)
                        for class_name in ("original_price", "original-price", "previous_price")
                    )
                    or node.tag in {"del", "s"}
                )
            )
            item_container = anchor.parent or anchor
            item_text = normalize_text(item_container.text())
            availability = (
                Availability.OUT_OF_STOCK
                if any(term in item_text for term in ("暂无供应", "暂时缺货", "缺货"))
                else Availability.ON_SALE
            )
            skus.append(
                {
                    "part_number": part_number,
                    "manufacturer_part_number": part_number,
                    "source_url": href,
                    "dimensions": dimensions,
                    "current_text": normalize_text(current.text()),
                    "original_text": normalize_text(original.text()) if original else None,
                    "original_label": self._original_label(original),
                    "availability": availability.value,
                }
            )

        if not skus:
            skus = self._selection_skus(result)
        if not skus:
            raise AppleParseError("Apple product page yielded no priced SKU")
        return ParsedCatalogProduct(
            external_product_id=item.external_product_id,
            category_code=item.category_code,
            brand_code=self.brand_code,
            name=name,
            series_name=name,
            rows=[self._catalog_row(item, name, sku) for sku in skus],
        )

    @classmethod
    def _selection_skus(cls, result: FetchResult) -> list[dict[str, object]]:
        try:
            page = result.body.decode("utf-8")
        except UnicodeDecodeError:
            return []
        marker = re.search(r"productSelectionData:\s*", page)
        if marker is None:
            return []
        try:
            data, _ = json.JSONDecoder().raw_decode(page, marker.end())
        except json.JSONDecodeError:
            return []
        if not isinstance(data, dict):
            return []
        products = data.get("products")
        main_values = data.get("mainDisplayValues")
        if not isinstance(products, list) or not isinstance(main_values, dict):
            return []
        prices = main_values.get("prices")
        if not isinstance(prices, dict):
            return []

        skus: list[dict[str, object]] = []
        seen: set[str] = set()
        for product in products:
            if not isinstance(product, dict):
                raise AppleParseError("Apple selection contains an invalid product entry")
            sku_id = normalize_text(
                str(product.get("btrOrFdPartNumber") or product.get("aosContainerPartNumber") or "")
            ).upper()
            price_key = normalize_text(str(product.get("priceKey", "")))
            price = prices.get(price_key)
            if not sku_id or not isinstance(price, dict):
                raise AppleParseError("Apple selection is missing a SKU identity or price")
            if sku_id in seen:
                raise AppleParseError(f"Apple product repeats SKU: {sku_id}")
            current = price.get("currentPrice")
            current_text = cls._bootstrap_price(current) or cls._bootstrap_price(
                price.get("amount")
            )
            if current_text is None:
                raise AppleParseError("Apple selection has no explicit current SKU amount")
            seen.add(sku_id)
            dimensions = cls._bootstrap_dimensions(product, main_values)
            configuration = product.get("productConfiguration")
            if isinstance(configuration, dict):
                dimensions.update(
                    {
                        f"configuration_{normalize_text(str(key)).lower()}": normalize_text(
                            str(value)
                        )
                        for key, value in configuration.items()
                        if str(value).strip()
                    }
                )
            container_part = cls._optional_string(product.get("aosContainerPartNumber"))
            if container_part:
                dimensions["aos_container_part_number"] = container_part
            original_text = cls._bootstrap_price(price.get("previousPrice"))
            skus.append(
                {
                    "part_number": sku_id,
                    "manufacturer_part_number": cls._optional_string(
                        product.get("btrOrFdPartNumber")
                    ),
                    "source_url": result.final_url,
                    "dimensions": dimensions,
                    "current_text": current_text,
                    "original_text": original_text,
                    "original_label": "Apple 划线原价" if original_text else None,
                    "availability": (
                        Availability.COMING_SOON
                        if product.get("isComingSoon") is True
                        else Availability.ON_SALE
                    ).value,
                }
            )
        return skus

    @classmethod
    def _bootstrap_dimensions(
        cls,
        product: dict[str, object],
        main_values: dict[str, object],
    ) -> dict[str, str]:
        raw_dimensions = product.get("dimensions")
        if not isinstance(raw_dimensions, dict):
            return {}
        dimensions: dict[str, str] = {}
        for raw_key, raw_value in raw_dimensions.items():
            key = normalize_text(str(raw_key))
            value = normalize_text(str(raw_value))
            choices = main_values.get(key)
            choice = choices.get(value) if isinstance(choices, dict) else None
            header = choice.get("header") if isinstance(choice, dict) else None
            display_value = cls._html_text(header) or value
            lower_key = key.lower()
            if "dimensioncolor" in lower_key:
                normalized_key = "color"
            elif "dimensionscreensize" in lower_key or "dimensioncasesize" in lower_key:
                normalized_key = "size"
            elif "processor" in lower_key:
                normalized_key = "processor"
            else:
                normalized_key = re.sub(r"[^a-z0-9]+", "_", lower_key).strip("_")
            if normalized_key and display_value:
                dimensions[normalized_key] = display_value
        return dimensions

    @staticmethod
    def _html_text(value: object) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        primary_label = re.split(r"<(?:div|as-footnote)\b", value, maxsplit=1)[0]
        return normalize_text(parse_html(primary_label).text()) or None

    @staticmethod
    def _bootstrap_price(value: object) -> str | None:
        if isinstance(value, dict):
            raw = value.get("raw_amount")
            if raw is None:
                raw = value.get("amount")
            return normalize_text(str(raw)) if raw is not None else None
        if isinstance(value, (int, float, str)) and str(value).strip():
            return normalize_text(str(value))
        return None

    def _catalog_row(
        self,
        product: DiscoveredCatalogProduct,
        product_name: str,
        raw: dict[str, object],
    ) -> ParsedCatalogRow:
        part_number = normalize_text(str(raw["part_number"])).upper()
        raw_dimensions = raw.get("dimensions")
        if not isinstance(raw_dimensions, dict):
            raise AppleParseError("Apple parsed SKU dimensions are invalid")
        dimensions = {
            normalize_text(str(key)).lower(): normalize_text(str(value))
            for key, value in raw_dimensions.items()
            if str(value).strip()
        }
        # Keep unrecognized, explicitly labelled configuration fields. The page
        # heading and prices never enter the specification fingerprint.
        known_dimensions = {
            "capacity",
            "storage",
            "memory",
            "color",
            "connectivity",
            "network",
            "size",
            "screensize",
            "casesize",
            "edition",
        }
        specification = DeviceSpecification(
            capacity=dimensions.get("capacity") or dimensions.get("storage"),
            memory=dimensions.get("memory"),
            color=dimensions.get("color"),
            connectivity=dimensions.get("connectivity") or dimensions.get("network"),
            size=dimensions.get("size")
            or dimensions.get("screensize")
            or dimensions.get("casesize"),
            edition=dimensions.get("edition"),
            manufacturer_part_number=self._optional_string(raw.get("manufacturer_part_number")),
            attributes={
                key: value for key, value in dimensions.items() if key not in known_dimensions
            },
        )
        original_text = self._optional_string(raw.get("original_text"))
        resolution = self.price_policy.resolve(
            original=PriceCandidate(
                original_text,
                self._optional_string(raw.get("original_label")) or "Apple 原价",
            )
            if original_text
            else None,
            current=PriceCandidate(str(raw["current_text"]), "Apple 当前售价"),
        )
        original_price = resolution.original_price
        original_type = resolution.original_price_type
        if original_price == resolution.current_price:
            original_price = None
            original_type = OriginalPriceType.NONE
        display_dimensions = (
            value for key, value in dimensions.items() if not key.startswith("configuration_")
        )
        sku_name = " ".join(
            part for part in (product_name, *dict.fromkeys(display_dimensions), part_number) if part
        )
        return ParsedCatalogRow(
            item=DiscoveredCatalogListing(
                listing_key=device_listing_key(
                    product_id=product.external_product_id,
                    sku_id=part_number,
                    specification=specification,
                ),
                url=str(raw["source_url"]),
                category_code=product.category_code,
                merchant=SourceMerchant(
                    merchant_key=self.channel_code,
                    name="Apple 中国大陆官方在线商店",
                    seller_type=SellerType.BRAND_OFFICIAL,
                    verification_status=VerificationStatus.VERIFIED,
                ),
                price_nature=PriceNature.RETAIL_OFFER,
                external_product_id=product.external_product_id,
                external_sku_id=part_number,
            ),
            parsed=ParsedCatalogListing(
                source_title=sku_name,
                source_category_path=product.category_code,
                source_attributes={"device_specification": specification.identity_attributes()},
                price_candidates=[
                    SourcePriceCandidate(
                        current_price=resolution.current_price,
                        original_price=original_price,
                        original_price_type=original_type,
                        price_type=PriceType.DIRECT_UNCONDITIONAL,
                        pricing_basis=PricingBasis.PACKAGE_TOTAL,
                        availability=Availability(str(raw["availability"])),
                        fee_status=FeeStatus.ITEM_ONLY,
                        displayed_price_text=str(raw["current_text"]),
                        region=CatalogRegion(scope=RegionScope.NATIONAL, code="CN"),
                    )
                ],
            ),
        )

    @classmethod
    def parse_discovery(
        cls,
        body: bytes,
        page_url: str,
        category_code: str,
    ) -> list[DiscoveredCatalogProduct]:
        cls._require_shop_path(page_url)
        root = parse_html(body)
        page_path = urlsplit(page_url).path.rstrip("/")
        items: dict[str, DiscoveredCatalogProduct] = {}
        for anchor in root.find_all(lambda node: node.tag == "a" and bool(node.attrs.get("href"))):
            href = urljoin(page_url, anchor.attrs["href"])
            parsed = urlsplit(href)
            if not cls._approved_url(href):
                continue
            candidate_path = parsed.path.rstrip("/")
            if not candidate_path.startswith(f"{page_path}/"):
                continue
            suffix = candidate_path[len(page_path) + 1 :]
            if not suffix or "/" in suffix or suffix in {"compare", "accessories"}:
                continue
            product_category = category_code
            if category_code == "COMPUTER":
                try:
                    product_category = cls._computer_category(suffix)
                except AppleParseError:
                    continue
            product_id = suffix.lower()
            items.setdefault(
                product_id,
                DiscoveredCatalogProduct(
                    external_product_id=product_id,
                    url=f"{APPLE_SHOP_ORIGIN}{candidate_path}",
                    category_code=product_category,
                ),
            )
        return list(items.values())

    @staticmethod
    def _computer_category(product_id: str) -> str:
        normalized = product_id.lower()
        if normalized.startswith("macbook"):
            return "LAPTOP"
        if normalized.startswith(("imac", "mac-mini", "mac-studio", "mac-pro")):
            return "DESKTOP"
        raise AppleParseError(f"out-of-scope Apple computer family: {product_id}")

    @classmethod
    def _category_from_url(cls, url: str, product_id: str) -> str:
        path = urlsplit(url).path
        if path.startswith("/shop/buy-iphone/"):
            return "PHONE"
        if path.startswith("/shop/buy-ipad/"):
            return "TABLET"
        if path.startswith("/shop/buy-watch/"):
            return "WATCH"
        if path.startswith("/shop/buy-mac/"):
            return cls._computer_category(product_id)
        return ""

    @classmethod
    def _part_number(cls, href: str, product_url: str) -> str | None:
        parsed = urlsplit(href)
        product_path = urlsplit(product_url).path.rstrip("/").lower()
        path = parsed.path.rstrip("/")
        if not cls._approved_url(href):
            return None
        if not path.lower().startswith(f"{product_path}/"):
            return None
        suffix = path[len(product_path) + 1 :].split("/")
        if len(suffix) != 2 or suffix[1].lower() != "a":
            return None
        if not re.fullmatch(r"[A-Za-z0-9]+", suffix[0]):
            return None
        return f"{suffix[0].upper()}/A"

    @staticmethod
    def _dimensions(anchor: HtmlNode) -> dict[str, str]:
        dimensions: dict[str, str] = {}
        for node in anchor.find_all(lambda candidate: candidate.tag in {"span", "div"}):
            for class_name in node.attrs.get("class", "").split():
                if not class_name.lower().startswith("dimension"):
                    continue
                key = class_name[len("dimension") :].lower()
                value = normalize_text(node.text())
                value = re.sub(r"\s*脚注\s*\d+.*$", "", value).strip()
                if key and value:
                    dimensions[key] = value
        return dimensions

    @staticmethod
    def _original_label(node: HtmlNode | None) -> str | None:
        if node is None:
            return None
        if node.tag in {"del", "s"} or any(
            token in node.attrs.get("class", "")
            for token in ("previous", "strikethrough", "crossed")
        ):
            return "Apple 划线原价"
        return "Apple 原价"

    @staticmethod
    def _approved_url(url: str) -> bool:
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme == "https"
            and parsed.hostname == "www.apple.com.cn"
            and port in {None, 443}
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
        )

    @classmethod
    def _require_shop_path(cls, url: str) -> None:
        parsed = urlsplit(url)
        if not cls._approved_url(url) or not re.fullmatch(
            r"/shop/buy-(?:iphone|ipad|mac|watch)(?:/[A-Za-z0-9-]+)?/?", parsed.path
        ):
            raise AppleParseError(f"Apple URL is outside approved shop paths: {url}")

    @classmethod
    def _require_same_path(cls, expected: str, actual: str) -> None:
        cls._require_shop_path(actual)
        if urlsplit(expected).path.rstrip("/") != urlsplit(actual).path.rstrip("/"):
            raise AppleParseError("Apple response URL does not match the discovered product path")

    @classmethod
    def _validate_product(cls, item: DiscoveredCatalogProduct) -> None:
        cls._require_shop_path(item.url)
        path = urlsplit(item.url).path.rstrip("/")
        if path.rsplit("/", 1)[-1] != item.external_product_id:
            raise AppleParseError("Apple product ID does not match its official URL")
        category = cls._category_from_url(item.url, item.external_product_id)
        if item.category_code not in cls.default_category_codes or item.category_code != category:
            raise AppleParseError("Apple product category does not match its official URL")

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return normalize_text(str(value)) if value is not None and str(value).strip() else None
