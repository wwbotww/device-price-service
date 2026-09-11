from __future__ import annotations

import json
import re
from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from device_price_service.crawlers.base import AdapterContext
from device_price_service.crawlers.catalog import CatalogConnector
from device_price_service.crawlers.html import HtmlNode, parse_html
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
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

VIVO_DISCOVERY_URL = "https://shop.vivo.com.cn/api/v1/home/index"
VIVO_INFO_PATH = "/api/v1/product/getInfo"
VIVO_DETAIL_PATH = "/api/v1/product/getDetail"
VIVO_SNAPSHOT_PLAN = BrowserSnapshotPlan(
    ready_selector=".product-main",
    snapshot_selector=".product-main",
    dimensions=(
        BrowserVariantDimension("version", ".sku-group", "版本", optional=True),
        BrowserVariantDimension("color", ".sku-group", "颜色", optional=True),
    ),
    settle_ms=350,
    max_snapshots=128,
)


class VivoParseError(ValueError):
    """Raised when a vivo Shop fixture no longer exposes required fields."""


class VivoCatalogConnector(CatalogConnector):
    brand_code = "VIVO"
    channel_code = "VIVO_CN_WEB"
    connector_code = "vivo-cn"
    version = "vivo-cn-catalog-product"
    fetch_method = CollectionFetchMethod.HTTP
    allowed_domains = ("shop.vivo.com.cn",)
    default_category_codes = ("PHONE", "TABLET", "WATCH")
    product_not_found_is_definitive = True

    def __init__(self, *, discovery_url: str = VIVO_DISCOVERY_URL) -> None:
        self.discovery_url = discovery_url
        self.price_policy = PricePolicy()

    async def discover_products(
        self, context: AdapterContext, request: CatalogCollectionRequest
    ) -> list[DiscoveredCatalogProduct]:
        if request.region_scope is not RegionScope.NATIONAL or request.region_code != "CN":
            raise VivoParseError("vivo official prices require NATIONAL/CN scope")
        categories = set(request.category_codes or self.default_category_codes)
        if request.source_item_codes or not categories.issubset(self.default_category_codes):
            raise VivoParseError("vivo request includes unsupported selections")
        self._require_origin(self.discovery_url)
        result = await context.http.fetch(
            self.discovery_url, allowed_domains=context.allowed_domains
        )
        self._require_same_url(self.discovery_url, result.request_url)
        self._require_same_url(self.discovery_url, result.final_url)
        if not 200 <= result.status_code < 300:
            raise VivoParseError(f"vivo discovery returned HTTP {result.status_code}")
        return [
            item
            for item in self.parse_discovery(result.body, result.final_url)
            if item.category_code in categories
        ]

    async def fetch_product(
        self, context: AdapterContext, item: DiscoveredCatalogProduct
    ) -> FetchResult:
        self._validate_product(item)
        if urlsplit(item.url).path == VIVO_INFO_PATH:
            info_result = await context.http.fetch(
                item.url,
                allowed_domains=context.allowed_domains,
            )
            self._require_same_url(item.url, info_result.request_url)
            self._require_same_url(item.url, info_result.final_url)
            if info_result.status_code in {404, 410}:
                return info_result
            if not 200 <= info_result.status_code < 300:
                raise VivoParseError(f"vivo info returned HTTP {info_result.status_code}")
            info_payload = self._api_payload(info_result.body)
            info = info_payload.get("data")
            if not isinstance(info, dict):
                raise VivoParseError("vivo getInfo schema is unsupported")
            spec_item = info.get("specItem")
            sku_ids = self._sku_ids(spec_item)
            details: dict[str, object] = {}
            evidence = [self._request_evidence(info_result)]
            fetched_at, duration_ms = info_result.fetched_at, info_result.duration_ms
            for sku_id in sku_ids:
                detail_url = self._detail_url(item.external_product_id, sku_id)
                detail_result = await context.http.fetch(
                    detail_url,
                    allowed_domains=context.allowed_domains,
                )
                self._require_same_url(detail_url, detail_result.request_url)
                self._require_same_url(detail_url, detail_result.final_url)
                if not 200 <= detail_result.status_code < 300:
                    raise VivoParseError(f"vivo detail returned HTTP {detail_result.status_code}")
                detail_payload = self._api_payload(detail_result.body)
                detail_data = detail_payload.get("data")
                raw = detail_data.get(sku_id) if isinstance(detail_data, dict) else None
                if not isinstance(raw, dict):
                    raise VivoParseError(f"vivo detail response omits requested SKU: {sku_id}")
                details[sku_id] = raw
                evidence.append(self._request_evidence(detail_result))
                fetched_at = max(fetched_at, detail_result.fetched_at)
                duration_ms += detail_result.duration_ms
            body = json.dumps(
                {
                    "schema": "vivo-api-detail-batch-v2",
                    "commoditySpu": info.get("commoditySpu"),
                    "specItem": spec_item,
                    "downSkuIds": info.get("downSkuIds", []),
                    "zeroStoreSkuIds": info.get("zeroStoreSkuIds", []),
                    "details": details,
                    "requests": evidence,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            return FetchResult(
                request_url=item.url,
                final_url=item.url,
                status_code=info_result.status_code,
                headers={
                    "content-type": "application/json",
                    "x-device-price-artifact": "vivo-api-detail-batch-v2",
                },
                body=body,
                fetched_at=fetched_at,
                duration_ms=duration_ms,
                fetch_method=info_result.fetch_method,
            )
        return await context.browser.fetch_snapshots(
            item.url, allowed_domains=context.allowed_domains, plan=VIVO_SNAPSHOT_PLAN
        )

    def parse_product(
        self, item: DiscoveredCatalogProduct, result: FetchResult
    ) -> ParsedCatalogProduct:
        self._validate_product(item)
        self._require_same_url(item.url, result.request_url)
        self._require_same_url(item.url, result.final_url)
        if not 200 <= result.status_code < 300:
            raise VivoParseError(f"vivo product returned HTTP {result.status_code}")
        if urlsplit(result.final_url).path == VIVO_INFO_PATH:
            return self._parse_api_product(item, result)
        name: str | None = None
        skus: list[dict[str, object]] = []
        seen: set[str] = set()
        for snapshot in self._snapshots(result.body, item.url):
            selections, root = self._snapshot_fields(snapshot)
            heading = root.first(lambda node: node.tag == "h1" or node.has_class("product-name"))
            current = root.first(
                lambda node: node.has_class("current-price") or node.has_class("sale-price")
            )
            if heading is None:
                raise VivoParseError("vivo product snapshot lacks name")
            snapshot_name = normalize_text(heading.text())
            name = name or snapshot_name
            if snapshot_name != name:
                raise VivoParseError("vivo variant snapshots contain different products")
            sku_id = self._sku_id(root)
            if sku_id in seen:
                raise VivoParseError(f"vivo product repeats SKU: {sku_id}")
            seen.add(sku_id)
            original = root.first(
                lambda node: (
                    node.tag in {"del", "s"}
                    or node.has_class("original-price")
                    or node.has_class("market-price")
                )
            )
            skus.append(
                {
                    "sku_id": sku_id,
                    "version": self._optional_string(selections.get("version")),
                    "color": self._optional_string(selections.get("color")),
                    "dimensions": selections,
                    "current_text": normalize_text(current.text()) if current else None,
                    "original_text": normalize_text(original.text()) if original else None,
                    "original_label": "vivo 商城划线原价" if original else None,
                    "availability": self._availability(root.text()).value,
                }
            )
        if name is None or not skus:
            raise VivoParseError("vivo snapshot evidence yielded no priced SKU")
        return self._catalog_product(item, name, skus)

    def _parse_api_product(
        self,
        item: DiscoveredCatalogProduct,
        result: FetchResult,
    ) -> ParsedCatalogProduct:
        try:
            envelope = json.loads(result.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise VivoParseError("vivo detail evidence is not valid JSON") from error
        if not isinstance(envelope, dict) or envelope.get("schema") != ("vivo-api-detail-batch-v2"):
            raise VivoParseError("vivo detail evidence schema is unsupported")
        product = envelope.get("commoditySpu")
        details = envelope.get("details")
        if not isinstance(product, dict) or not isinstance(details, dict):
            raise VivoParseError("vivo detail evidence schema is unsupported")
        if normalize_text(str(product.get("id", ""))) != item.external_product_id:
            raise VivoParseError("vivo detail evidence contains a different product")
        name = normalize_text(str(product.get("spuName", "")))
        if not name:
            raise VivoParseError("vivo detail evidence lacks a product name")
        specs = self._sku_specs(envelope.get("specItem"))
        if set(details) != set(specs):
            raise VivoParseError("vivo detail evidence does not cover the advertised SKU map")
        down = self._status_sku_ids(envelope.get("downSkuIds", []))
        zero = self._status_sku_ids(envelope.get("zeroStoreSkuIds", []))
        skus: list[dict[str, object]] = []
        for sku_id, raw in details.items():
            if not isinstance(raw, dict):
                raise VivoParseError("vivo detail evidence has an invalid SKU entry")
            normalized_sku_id = normalize_text(str(sku_id))
            attributes = specs.get(normalized_sku_id, {})
            version = next(
                (value for key, value in attributes.items() if key in {"版本", "规格", "容量"}),
                None,
            )
            color = attributes.get("颜色") or self._optional_string(raw.get("colorName"))
            skus.append(
                {
                    "sku_id": normalized_sku_id,
                    "version": version,
                    "color": color,
                    "dimensions": attributes,
                    "current_text": (
                        str(raw["salePrice"]) if raw.get("salePrice") is not None else None
                    ),
                    "original_text": self._optional_string(raw.get("marketPrice")),
                    "original_label": "vivo 商城划线原价",
                    "availability": self._api_availability(
                        normalized_sku_id,
                        raw,
                        down,
                        zero,
                    ).value,
                    "source_url": self._detail_url(
                        item.external_product_id,
                        normalized_sku_id,
                    ),
                }
            )
        if not skus:
            raise VivoParseError("vivo detail evidence yielded no directly priced SKU")
        return self._catalog_product(item, name, skus)

    def _catalog_product(
        self, item: DiscoveredCatalogProduct, name: str, skus: list[dict[str, object]]
    ) -> ParsedCatalogProduct:
        if self._category_for_name(name) != item.category_code:
            raise VivoParseError("vivo product is outside the approved brand or categories")
        return ParsedCatalogProduct(
            brand_code=self.brand_code,
            category_code=item.category_code,
            external_product_id=item.external_product_id,
            name=name,
            series_name=name,
            rows=[self._catalog_row(item, name, raw) for raw in skus],
        )

    def _catalog_row(
        self, item: DiscoveredCatalogProduct, name: str, raw: dict[str, object]
    ) -> ParsedCatalogRow:
        version = self._optional_string(raw.get("version"))
        color = self._optional_string(raw.get("color"))
        memory, capacity = self._memory_capacity(version)
        dimensions = raw.get("dimensions")
        dimensions = dimensions if isinstance(dimensions, dict) else {}
        attributes = {
            str(key): normalize_text(str(value))
            for key, value in dimensions.items()
            if value is not None and str(value).strip()
        }
        specification = DeviceSpecification(
            color=color,
            memory=memory or attributes.get("内存"),
            capacity=capacity or attributes.get("容量"),
            edition=version,
            connectivity=attributes.get("网络") or attributes.get("connectivity"),
            size=attributes.get("尺寸") or attributes.get("size"),
            attributes={
                key: value
                for key, value in attributes.items()
                if key
                not in {
                    "color",
                    "颜色",
                    "version",
                    "版本",
                    "网络",
                    "connectivity",
                    "尺寸",
                    "size",
                }
            },
        )
        sku_id = normalize_text(str(raw["sku_id"]))
        source_url = self._optional_string(raw.get("source_url")) or item.url
        self._require_same_url(
            self._detail_url(item.external_product_id, sku_id)
            if urlsplit(source_url).path == VIVO_DETAIL_PATH
            else item.url,
            source_url,
        )
        availability = Availability(str(raw["availability"]))
        if raw.get("current_text") is None:
            if availability not in {
                Availability.OFF_SHELF,
                Availability.OUT_OF_STOCK,
                Availability.COMING_SOON,
            }:
                raise VivoParseError("vivo SKU lacks direct price or explicit unavailable state")
            candidate = SourcePriceCandidate(
                price_type=PriceType.AVAILABILITY_ONLY,
                pricing_basis=PricingBasis.UNKNOWN,
                availability=availability,
                fee_status=FeeStatus.NOT_APPLICABLE,
            )
        else:
            original, original_type, current = self._resolve_price(raw)
            candidate = SourcePriceCandidate(
                current_price=current,
                original_price=original,
                original_price_type=original_type,
                price_type=PriceType.DIRECT_UNCONDITIONAL,
                pricing_basis=PricingBasis.PACKAGE_TOTAL,
                availability=availability,
                fee_status=FeeStatus.ITEM_ONLY,
                displayed_price_text=str(raw["current_text"]),
            )
        return ParsedCatalogRow(
            item=DiscoveredCatalogListing(
                listing_key=device_listing_key(
                    product_id=item.external_product_id, sku_id=sku_id, specification=specification
                ),
                url=source_url,
                category_code=item.category_code,
                merchant=SourceMerchant(
                    merchant_key=self.channel_code,
                    name="vivo 中国大陆官方商城",
                    seller_type=SellerType.BRAND_OFFICIAL,
                    verification_status=VerificationStatus.VERIFIED,
                ),
                price_nature=PriceNature.RETAIL_OFFER,
                external_product_id=item.external_product_id,
                external_sku_id=sku_id,
            ),
            parsed=ParsedCatalogListing(
                source_title=" ".join(part for part in (name, version, color) if part),
                source_category_path=item.category_code,
                source_attributes={"device_specification": specification.identity_attributes()},
                price_candidates=[candidate],
            ),
        )

    @classmethod
    def parse_discovery(cls, body: bytes, page_url: str) -> list[DiscoveredCatalogProduct]:
        cls._require_origin(page_url)
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, (dict, list)):
            return cls._parse_json_discovery(payload)

        root = parse_html(body)
        items: dict[str, DiscoveredCatalogProduct] = {}
        for anchor in root.find_all(lambda node: node.tag == "a" and bool(node.attrs.get("href"))):
            href = urljoin(page_url, anchor.attrs["href"])
            parsed = urlsplit(href)
            match = re.fullmatch(r"/(?:wap/)?product/(\d+)", parsed.path.rstrip("/"))
            if parsed.scheme != "https" or parsed.hostname != "shop.vivo.com.cn" or match is None:
                continue
            name = normalize_text(anchor.text())
            category = cls._category_for_name(name)
            if category is None:
                continue
            product_id = match.group(1)
            items.setdefault(
                product_id,
                DiscoveredCatalogProduct(
                    external_product_id=product_id,
                    url=f"https://shop.vivo.com.cn/product/{product_id}",
                    category_code=category,
                    metadata={"discovered_name": name},
                ),
            )
        return list(items.values())

    @classmethod
    def _parse_json_discovery(cls, payload: object) -> list[DiscoveredCatalogProduct]:
        if isinstance(payload, dict) and "code" in payload and payload["code"] != 0:
            raise VivoParseError("vivo discovery API response is unsuccessful")
        items: dict[str, DiscoveredCatalogProduct] = {}

        def visit(value: object) -> None:
            if isinstance(value, list):
                for child in value:
                    visit(child)
                return
            if not isinstance(value, dict):
                return
            product_id = normalize_text(str(value.get("spuId", "")))
            sku_id = normalize_text(str(value.get("skuId", "")))
            name = normalize_text(str(value.get("name") or ""))
            category = cls._category_for_name(name)
            if product_id.isdigit() and sku_id.isdigit() and category is not None:
                items.setdefault(
                    product_id,
                    DiscoveredCatalogProduct(
                        external_product_id=product_id,
                        url=cls._info_url(product_id),
                        category_code=category,
                        metadata={"discovered_name": name, "seed_sku_id": sku_id},
                    ),
                )
            for child in value.values():
                visit(child)

        visit(payload)
        return list(items.values())

    @classmethod
    def _validate_product(cls, item: DiscoveredCatalogProduct) -> None:
        cls._require_product_path(item.url)
        if (
            not item.external_product_id.isdigit()
            or item.category_code not in cls.default_category_codes
        ):
            raise VivoParseError("vivo product identity or category is invalid")
        parsed = urlsplit(item.url)
        product_id = (
            parse_qs(parsed.query)["spuId"][0]
            if parsed.path == VIVO_INFO_PATH
            else parsed.path.rstrip("/").rsplit("/", 1)[-1]
        )
        if product_id != item.external_product_id:
            raise VivoParseError("vivo product URL disagrees with product identity")

    @staticmethod
    def _require_origin(url: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "shop.vivo.com.cn"
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
        ):
            raise VivoParseError("vivo URL is outside the official origin")

    @classmethod
    def _require_same_url(cls, expected: str, actual: str) -> None:
        cls._require_origin(actual)
        first, second = urlsplit(expected), urlsplit(actual)
        if first.path != second.path or parse_qs(first.query) != parse_qs(second.query):
            raise VivoParseError("vivo response URL differs from its request")

    @staticmethod
    def _request_evidence(result: FetchResult) -> dict[str, object]:
        return {
            "request_url": result.request_url,
            "final_url": result.final_url,
            "status_code": result.status_code,
            "fetched_at": result.fetched_at.isoformat(),
            "source_hash": result.source_hash,
            "body": result.body.decode("utf-8"),
        }

    @staticmethod
    def _status_sku_ids(value: object) -> set[str]:
        if not isinstance(value, list):
            raise VivoParseError("vivo unavailable SKU list is invalid")
        return {normalize_text(str(sku)) for sku in value}

    @staticmethod
    def _api_payload(body: bytes) -> dict[str, object]:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise VivoParseError("vivo API response is not valid JSON") from error
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise VivoParseError("vivo API response is unsuccessful")
        return payload

    @staticmethod
    def _sku_ids(spec_item: object) -> list[str]:
        if not isinstance(spec_item, dict):
            raise VivoParseError("vivo product has no SKU specification map")
        values = spec_item.get("skuSpecList")
        if not isinstance(values, list):
            raise VivoParseError("vivo product has no SKU specification map")
        result: list[str] = []
        for entry in values:
            sku_id = normalize_text(str(entry.get("skuId", ""))) if isinstance(entry, dict) else ""
            if not sku_id.isdigit() or sku_id in result:
                raise VivoParseError("vivo SKU map contains invalid or duplicate identities")
            result.append(sku_id)
        if not result or len(result) > 128:
            raise VivoParseError("vivo SKU count is empty or exceeds the safety limit")
        return result

    @classmethod
    def _sku_specs(cls, spec_item: object) -> dict[str, dict[str, str]]:
        cls._sku_ids(spec_item)
        if not isinstance(spec_item, dict):
            raise VivoParseError("vivo product has no specification map")
        labels = spec_item.get("specMainSeq")
        values = spec_item.get("specItemSeq")
        entries = spec_item.get("skuSpecList")
        if (
            not isinstance(labels, dict)
            or not isinstance(values, dict)
            or not isinstance(entries, list)
        ):
            raise VivoParseError("vivo specification dimensions are incomplete")
        result: dict[str, dict[str, str]] = {}
        for entry in entries:
            sequences = entry.get("sequences") if isinstance(entry, dict) else None
            sku_id = normalize_text(str(entry.get("skuId", ""))) if isinstance(entry, dict) else ""
            if not sku_id or not isinstance(sequences, dict) or set(sequences) != set(labels):
                raise VivoParseError("vivo SKU omits required specification dimensions")
            attributes: dict[str, str] = {}
            for dimension, sequence in sequences.items():
                label = normalize_text(str(labels.get(dimension, "")))
                options = values.get(dimension)
                try:
                    index = int(str(sequence)) - 1
                    option = options[index] if isinstance(options, list) and index >= 0 else None
                except (ValueError, IndexError):
                    option = None
                name = (
                    normalize_text(str(option.get("name", ""))) if isinstance(option, dict) else ""
                )
                if not label or not name or label in attributes:
                    raise VivoParseError("vivo SKU has unresolved specification dimensions")
                attributes[label] = name
            if not attributes:
                raise VivoParseError("vivo SKU has no proven specifications")
            result[sku_id] = attributes
        return result

    @staticmethod
    def _api_availability(
        sku_id: str,
        raw: dict[str, object],
        down: set[str],
        zero: set[str],
    ) -> Availability:
        if sku_id in down or raw.get("marketable") == 0:
            return Availability.OFF_SHELF
        status = raw.get("skuStatus")
        has_store = status.get("hasStore") if isinstance(status, dict) else None
        if sku_id in zero or has_store == 0:
            return Availability.OUT_OF_STOCK
        if has_store == 1 and raw.get("marketable") == 1:
            return Availability.ON_SALE
        return Availability.UNKNOWN

    @staticmethod
    def _info_url(product_id: str) -> str:
        return f"https://shop.vivo.com.cn{VIVO_INFO_PATH}?{urlencode({'spuId': product_id})}"

    @staticmethod
    def _detail_url(product_id: str, sku_id: str) -> str:
        query = urlencode(
            {
                "spuId": product_id,
                "typeId": "1",
                "skuId": sku_id,
                "needSurfRecord": "true",
            }
        )
        return f"https://shop.vivo.com.cn{VIVO_DETAIL_PATH}?{query}"

    @staticmethod
    def _category_for_name(name: str) -> str | None:
        value = normalize_text(name)
        upper = value.upper()
        if upper.startswith("IQOO") or upper.startswith("NEX"):
            return None
        if any(
            term in value
            for term in (
                "耳机",
                "手环",
                "充电",
                "数据线",
                "保护壳",
                "保护膜",
                "贴膜",
                "碎屏宝",
                "键盘",
                "触控笔",
                "表带",
                "底座",
            )
        ):
            return None
        if re.match(r"^VIVO\s+PAD", upper):
            return "TABLET"
        if re.match(r"^VIVO\s+WATCH", upper):
            return "WATCH"
        if re.match(r"^VIVO\s+(?:X|S|Y|V)\w*", upper):
            return "PHONE"
        return None

    @classmethod
    def _snapshots(cls, body: bytes, source_url: str) -> list[object]:
        try:
            envelope = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise VivoParseError("vivo snapshot evidence is not valid JSON") from error
        snapshots = envelope.get("snapshots") if isinstance(envelope, dict) else None
        if (
            not isinstance(envelope, dict)
            or envelope.get("schema") != "device-price-browser-snapshots-v1"
            or not isinstance(snapshots, list)
            or not snapshots
        ):
            raise VivoParseError("vivo snapshot evidence schema is unsupported")
        cls._require_same_url(source_url, str(envelope.get("source_url", "")))
        return snapshots

    @staticmethod
    def _snapshot_fields(snapshot: object) -> tuple[dict[str, object], HtmlNode]:
        if (
            not isinstance(snapshot, dict)
            or not isinstance(snapshot.get("selections"), dict)
            or not isinstance(snapshot.get("html"), str)
        ):
            raise VivoParseError("vivo snapshot entry is invalid")
        return snapshot["selections"], parse_html(snapshot["html"])

    @staticmethod
    def _sku_id(root: HtmlNode) -> str:
        node = root.first(lambda candidate: bool(candidate.attrs.get("data-sku-id")))
        if node is None:
            raise VivoParseError("vivo product snapshot has no official SKU ID")
        return normalize_text(node.attrs["data-sku-id"])

    def _resolve_price(
        self, raw: dict[str, object]
    ) -> tuple[Decimal | None, OriginalPriceType, Decimal]:
        original_text = self._optional_string(raw.get("original_text"))
        resolution = self.price_policy.resolve(
            original=PriceCandidate(
                original_text, self._optional_string(raw.get("original_label")) or "vivo 商城原价"
            )
            if original_text
            else None,
            current=PriceCandidate(str(raw["current_text"]), "vivo 商城当前售价"),
        )
        original_price = resolution.original_price
        original_type = resolution.original_price_type
        if original_price == resolution.current_price:
            original_price = None
            original_type = OriginalPriceType.NONE
        if resolution.current_price is None:
            raise VivoParseError("vivo current price is missing")
        return original_price, original_type, resolution.current_price

    @staticmethod
    def _memory_capacity(version: str | None) -> tuple[str | None, str | None]:
        match = re.search(r"(\d+\s*GB)\s*\+\s*(\d+\s*(?:GB|TB))", version or "", re.I)
        return (
            (normalize_capacity(match.group(1)), normalize_capacity(match.group(2)))
            if match
            else (None, None)
        )

    @staticmethod
    def _availability(text: str) -> Availability:
        if "商品已下架" in text:
            return Availability.OFF_SHELF
        if any(term in text for term in ("暂时缺货", "已售罄", "到货通知")):
            return Availability.OUT_OF_STOCK
        if "预约" in text:
            return Availability.RESERVATION
        if "预售" in text:
            return Availability.PRE_SALE
        if any(term in text for term in ("立即购买", "加入购物车", "现货")):
            return Availability.ON_SALE
        return Availability.UNKNOWN

    @classmethod
    def _require_product_path(cls, url: str) -> None:
        cls._require_origin(url)
        parsed = urlsplit(url)
        legacy = re.fullmatch(r"/(?:wap/)?product/\d+", parsed.path.rstrip("/")) is not None
        query = parse_qs(parsed.query)
        current = (
            parsed.path == VIVO_INFO_PATH
            and len(query.get("spuId", [])) == 1
            and query["spuId"][0].isdigit()
        )
        if (
            parsed.scheme != "https"
            or parsed.hostname != "shop.vivo.com.cn"
            or not (legacy or current)
        ):
            raise VivoParseError(f"vivo URL is outside approved product paths: {url}")

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return normalize_text(str(value)) if value is not None and str(value).strip() else None
