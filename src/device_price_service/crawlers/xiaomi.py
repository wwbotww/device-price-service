from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from device_price_service.crawlers.base import AdapterContext
from device_price_service.crawlers.catalog import CatalogConnector
from device_price_service.crawlers.html import parse_html
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
from device_price_service.domain.crawl import (
    BrowserSnapshotPlan,
    BrowserVariantDimension,
    FetchResult,
)
from device_price_service.domain.price_policy import PriceCandidate, PricePolicy
from device_price_service.normalization.devices import DeviceSpecification, device_listing_key
from device_price_service.normalization.specs import (
    normalize_capacity,
    normalize_text,
)

XIAOMI_SHOP_URL = "https://www.mi.com/shop/"
XIAOMI_SNAPSHOT_PLAN = BrowserSnapshotPlan(
    ready_selector=".product-con .price-info",
    snapshot_selector=".product-con",
    dimensions=(
        BrowserVariantDimension(
            name="color",
            container_selector=".buy-option .option-box",
            heading_text="选择颜色",
        ),
        BrowserVariantDimension(
            name="version",
            container_selector=".buy-option .option-box",
            heading_text=("选择规格", "选择版本"),
            optional=True,
        ),
    ),
    settle_ms=350,
    max_snapshots=96,
)


class XiaomiParseError(ValueError):
    """Raised when a Xiaomi shop fixture no longer exposes required SKU fields."""


class XiaomiCatalogConnector(CatalogConnector):
    brand_code = "XIAOMI"
    channel_code = "XIAOMI_CN_WEB"
    connector_code = "xiaomi-cn"
    version = "xiaomi-cn-catalog-product"
    fetch_method = CollectionFetchMethod.BROWSER
    allowed_domains = ("www.mi.com",)
    default_category_codes = ("PHONE", "TABLET", "LAPTOP", "WATCH")
    product_not_found_is_definitive = True

    def __init__(self, *, discovery_url: str = XIAOMI_SHOP_URL) -> None:
        self.discovery_url = discovery_url
        self.price_policy = PricePolicy()

    async def discover_products(
        self, context: AdapterContext, request: CatalogCollectionRequest
    ) -> list[DiscoveredCatalogProduct]:
        if request.region_scope is not RegionScope.NATIONAL or request.region_code != "CN":
            raise XiaomiParseError("Xiaomi official prices require NATIONAL/CN scope")
        if request.source_item_codes:
            raise XiaomiParseError("Xiaomi discovery does not accept commodity selections")
        categories = set(request.category_codes or self.default_category_codes)
        if not categories.issubset(self.default_category_codes):
            raise XiaomiParseError("Xiaomi request includes an unsupported device category")
        result = await context.http.fetch(
            self.discovery_url,
            allowed_domains=context.allowed_domains,
        )
        if result.request_url != self.discovery_url or result.final_url != self.discovery_url:
            raise XiaomiParseError("Xiaomi discovery response changed its requested URL")
        if result.status_code < 200 or result.status_code >= 300:
            raise XiaomiParseError(f"Xiaomi discovery returned HTTP {result.status_code}")
        return [
            item
            for item in self.parse_discovery(result.body, result.final_url)
            if item.category_code in categories
        ]

    async def fetch_product(
        self,
        context: AdapterContext,
        item: DiscoveredCatalogProduct,
    ) -> FetchResult:
        self._validate_product(item)
        return await context.browser.fetch_snapshots(
            item.url,
            allowed_domains=context.allowed_domains,
            plan=XIAOMI_SNAPSHOT_PLAN,
        )

    def parse_product(
        self, item: DiscoveredCatalogProduct, result: FetchResult
    ) -> ParsedCatalogProduct:
        self._validate_product(item)
        for url in (result.request_url, result.final_url):
            if self._require_shop_path(url) != item.external_product_id:
                raise XiaomiParseError("Xiaomi response belongs to a different product")
        if not 200 <= result.status_code < 300:
            raise XiaomiParseError(f"Xiaomi product returned HTTP {result.status_code}")
        try:
            envelope = json.loads(result.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise XiaomiParseError("Xiaomi snapshot evidence is not valid JSON") from error
        if not isinstance(envelope, dict) or envelope.get("schema") != (
            "device-price-browser-snapshots-v1"
        ):
            raise XiaomiParseError("Xiaomi snapshot evidence schema is unsupported")
        source_url = envelope.get("source_url")
        if (
            not isinstance(source_url, str)
            or self._require_shop_path(source_url) != item.external_product_id
        ):
            raise XiaomiParseError("Xiaomi snapshot source belongs to a different product")
        snapshots = envelope.get("snapshots")
        if not isinstance(snapshots, list) or not snapshots:
            raise XiaomiParseError("Xiaomi snapshot evidence contains no variants")

        name: str | None = None
        parsed_skus: list[dict[str, object]] = []
        seen_specs: set[tuple[tuple[str, str], ...]] = set()
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                raise XiaomiParseError("Xiaomi snapshot entry is invalid")
            selections = snapshot.get("selections")
            html = snapshot.get("html")
            if not isinstance(selections, dict) or not isinstance(html, str):
                raise XiaomiParseError("Xiaomi snapshot fields are invalid")
            if any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or not key.strip()
                or not value.strip()
                for key, value in selections.items()
            ):
                raise XiaomiParseError("Xiaomi snapshot has invalid configuration dimensions")
            version = normalize_text(str(selections.get("version", "")))
            color = normalize_text(str(selections.get("color", "")))
            if not color:
                raise XiaomiParseError("Xiaomi snapshot is missing color")
            key = tuple(
                sorted((normalize_text(k), normalize_text(v)) for k, v in selections.items())
            )
            if key in seen_specs:
                raise XiaomiParseError("Xiaomi product repeats a configuration")
            seen_specs.add(key)

            root = parse_html(html)
            heading = root.first(lambda node: node.tag == "h2")
            price_box = root.first(lambda node: node.has_class("price-info"))
            if heading is None:
                raise XiaomiParseError("Xiaomi rendered product fragment lacks name")
            snapshot_name = normalize_text(heading.text())
            name = name or snapshot_name
            if snapshot_name != name:
                raise XiaomiParseError("Xiaomi variant snapshots contain different products")

            original_node = (
                price_box.first(
                    lambda node: (
                        node.tag in {"del", "s"}
                        or node.has_class("original-price")
                        or node.has_class("market-price")
                    )
                )
                if price_box
                else None
            )
            current_node = (
                price_box.first(lambda node: node.has_class("current-price")) if price_box else None
            )
            if current_node is None and price_box is not None:
                current_node = price_box.first(
                    lambda node: (
                        node.tag in {"span", "strong"}
                        and not node.has_class("original-price")
                        and not node.has_class("market-price")
                    )
                )
            current_text = (
                normalize_text(" ".join(current_node.text_parts)) if current_node else None
            )
            if not current_text and current_node:
                current_text = normalize_text(current_node.text())

            stock_node = root.first(lambda node: node.has_class("address-box"))
            stock_text = normalize_text(stock_node.text()) if stock_node else ""
            parsed_skus.append(
                {
                    "version": version,
                    "color": color,
                    "dimensions": selections,
                    "current_text": current_text,
                    "original_text": normalize_text(original_node.text())
                    if original_node
                    else None,
                    "original_label": "小米商城划线原价" if original_node else None,
                    "availability": self._availability(stock_text).value,
                }
            )

        if name is None or not parsed_skus:
            raise XiaomiParseError("Xiaomi snapshot evidence yielded no priced SKU")
        if self._category_for_name(name) != item.category_code:
            raise XiaomiParseError("Xiaomi product name no longer matches discovered category")
        return ParsedCatalogProduct(
            external_product_id=item.external_product_id,
            category_code=item.category_code,
            brand_code=self.brand_code,
            name=name,
            series_name=name,
            rows=[self._catalog_row(item, result.final_url, name, sku) for sku in parsed_skus],
        )

    def _catalog_row(
        self,
        item: DiscoveredCatalogProduct,
        source_url: str,
        product_name: str,
        raw: dict[str, object],
    ) -> ParsedCatalogRow:
        version = normalize_text(str(raw["version"]))
        color = normalize_text(str(raw["color"]))
        dimensions = raw["dimensions"]
        if not isinstance(dimensions, dict):
            raise XiaomiParseError("Xiaomi parsed configuration is invalid")
        memory, capacity, edition = self._version_attributes(version)
        specification = DeviceSpecification(
            color=color,
            memory=memory,
            capacity=capacity,
            edition=edition,
            attributes={
                str(key): str(value) for key, value in dimensions.items() if key != "color"
            },
        )
        return ParsedCatalogRow(
            item=DiscoveredCatalogListing(
                listing_key=device_listing_key(
                    product_id=item.external_product_id,
                    sku_id=None,
                    specification=specification,
                ),
                url=source_url,
                category_code=item.category_code,
                external_product_id=item.external_product_id,
                merchant=SourceMerchant(
                    merchant_key=self.channel_code,
                    name="小米中国大陆官方商城",
                    seller_type=SellerType.BRAND_OFFICIAL,
                    verification_status=VerificationStatus.VERIFIED,
                ),
                price_nature=PriceNature.RETAIL_OFFER,
            ),
            parsed=ParsedCatalogListing(
                source_title=" ".join(part for part in (product_name, version, color) if part),
                source_category_path=item.category_code,
                source_attributes={"device_specification": specification.identity_attributes()},
                price_candidates=[self._price_candidate(raw)],
            ),
        )

    def _price_candidate(self, raw: dict[str, object]) -> SourcePriceCandidate:
        current_text = self._optional_string(raw.get("current_text"))
        availability = Availability(str(raw["availability"]))
        region = CatalogRegion(scope=RegionScope.NATIONAL, code="CN")
        if current_text is None:
            if availability not in {
                Availability.OFF_SHELF,
                Availability.OUT_OF_STOCK,
                Availability.COMING_SOON,
            }:
                raise XiaomiParseError(
                    "Xiaomi SKU has no direct price or explicit unavailable state"
                )
            return SourcePriceCandidate(
                price_type=PriceType.AVAILABILITY_ONLY,
                pricing_basis=PricingBasis.UNKNOWN,
                availability=availability,
                fee_status=FeeStatus.NOT_APPLICABLE,
                region=region,
            )
        original_text = self._optional_string(raw.get("original_text"))
        resolution = self.price_policy.resolve(
            original=PriceCandidate(original_text, "小米商城划线原价") if original_text else None,
            current=PriceCandidate(current_text, "小米商城当前售价"),
        )
        original_price = resolution.original_price
        original_type = resolution.original_price_type
        if original_price == resolution.current_price:
            original_price = None
            original_type = OriginalPriceType.NONE
        return SourcePriceCandidate(
            current_price=resolution.current_price,
            original_price=original_price,
            original_price_type=original_type,
            price_type=PriceType.DIRECT_UNCONDITIONAL,
            pricing_basis=PricingBasis.PACKAGE_TOTAL,
            availability=availability,
            fee_status=FeeStatus.ITEM_ONLY,
            displayed_price_text=current_text,
            region=region,
        )

    @classmethod
    def parse_discovery(cls, body: bytes, page_url: str) -> list[DiscoveredCatalogProduct]:
        root = parse_html(body)
        items: dict[str, DiscoveredCatalogProduct] = {}
        for anchor in root.find_all(lambda node: node.tag == "a" and bool(node.attrs.get("href"))):
            href = urljoin(page_url, anchor.attrs["href"])
            parsed = urlsplit(href)
            if parsed.scheme != "https" or parsed.hostname != "www.mi.com":
                continue
            if parsed.path not in {"/shop/buy", "/shop/buy/detail"}:
                continue
            product_ids = parse_qs(parsed.query).get("product_id", [])
            if len(product_ids) != 1 or not product_ids[0].isdigit():
                continue
            name = normalize_text(anchor.text())
            name = re.sub(r"\s+[0-9,.]+\s*元(?:起)?\s*$", "", name)
            category_code = cls._category_for_name(name)
            if category_code is None:
                continue
            product_id = product_ids[0]
            items.setdefault(
                product_id,
                DiscoveredCatalogProduct(
                    external_product_id=product_id,
                    url=f"https://www.mi.com/shop/buy/detail?product_id={product_id}",
                    category_code=category_code,
                    metadata={"discovered_name": name},
                ),
            )
        return list(items.values())

    @staticmethod
    def _category_for_name(name: str) -> str | None:
        normalized = normalize_text(name)
        upper = normalized.upper()
        if upper.startswith("REDMI") or "红米" in normalized:
            return None
        if any(term in normalized for term in ("手环", "耳机", "配件", "保护壳", "键盘", "触控笔")):
            return None
        if upper.startswith("XIAOMI PAD") or normalized.startswith("小米平板"):
            return "TABLET"
        if upper.startswith("XIAOMI BOOK") or normalized.startswith("小米笔记本"):
            return "LAPTOP"
        if upper.startswith("XIAOMI WATCH") or normalized.startswith("小米手表"):
            return "WATCH"
        if re.match(r"^(XIAOMI\s+(?:\d|MIX|CIVI)|小米\s*\d)", upper):
            return "PHONE"
        return None

    @staticmethod
    def _version_attributes(version: str) -> tuple[str | None, str | None, str | None]:
        if not version:
            return None, None, None
        match = re.search(
            r"(?P<memory>\d+\s*GB)\s*(?:\+|/)\s*(?P<capacity>\d+\s*(?:GB|TB))",
            version,
            re.I,
        )
        if match is None:
            return None, None, version
        memory = normalize_capacity(match.group("memory"))
        capacity = normalize_capacity(match.group("capacity"))
        edition = (
            normalize_text(
                f"{version[: match.start()].strip(' /+')} {version[match.end() :].strip(' /+')}"
            )
            or None
        )
        return memory, capacity, edition

    @staticmethod
    def _availability(value: str) -> Availability:
        if "已下架" in value or "商品下架" in value:
            return Availability.OFF_SHELF
        if "即将开售" in value or "即将上市" in value:
            return Availability.COMING_SOON
        if any(term in value for term in ("缺货", "无货", "暂时没有")):
            return Availability.OUT_OF_STOCK
        if "预约" in value:
            return Availability.RESERVATION
        if "预售" in value:
            return Availability.PRE_SALE
        if "有现货" in value or "现货" in value:
            return Availability.ON_SALE
        return Availability.UNKNOWN

    @staticmethod
    def _require_shop_path(url: str) -> str:
        parsed = urlsplit(url)
        product_ids = parse_qs(parsed.query).get("product_id", [])
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.mi.com"
            or parsed.path not in {"/shop/buy", "/shop/buy/detail"}
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or len(product_ids) != 1
            or not product_ids[0].isdigit()
        ):
            raise XiaomiParseError(f"Xiaomi URL is outside approved shop paths: {url}")
        return product_ids[0]

    @classmethod
    def _validate_product(cls, item: DiscoveredCatalogProduct) -> None:
        if cls._require_shop_path(item.url) != item.external_product_id:
            raise XiaomiParseError("Xiaomi product URL disagrees with discovered identity")
        if item.category_code not in cls.default_category_codes:
            raise XiaomiParseError("Xiaomi product category is outside the approved scope")

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return normalize_text(str(value)) if value is not None and str(value).strip() else None
