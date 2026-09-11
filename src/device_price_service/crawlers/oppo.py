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

OPPO_DISCOVERY_URL = "https://www.opposhop.cn/cn/oapi/goods/web/products/v15/040204"
OPPO_DETAIL_PATH = "/cn/oapi/cms-business/goods/detail"
OPPO_SNAPSHOT_PLAN = BrowserSnapshotPlan(
    ready_selector=".product-detail",
    snapshot_selector=".product-detail",
    dimensions=(
        BrowserVariantDimension("version", ".sku-option-group", "版本", optional=True),
        BrowserVariantDimension("color", ".sku-option-group", "颜色", optional=True),
    ),
    settle_ms=350,
    max_snapshots=128,
)


class OppoParseError(ValueError):
    """Raised when an OPPO Shop fixture no longer exposes required fields."""


class OppoCatalogConnector(CatalogConnector):
    brand_code = "OPPO"
    channel_code = "OPPO_CN_WEB"
    connector_code = "oppo-cn"
    version = "oppo-cn-catalog-product"
    fetch_method = CollectionFetchMethod.HTTP
    allowed_domains = ("www.opposhop.cn",)
    default_category_codes = ("PHONE", "TABLET", "WATCH")

    def __init__(self, *, discovery_url: str = OPPO_DISCOVERY_URL) -> None:
        self.discovery_url = discovery_url
        self.price_policy = PricePolicy()

    async def discover_products(
        self, context: AdapterContext, request: CatalogCollectionRequest
    ) -> list[DiscoveredCatalogProduct]:
        if request.region_scope is not RegionScope.NATIONAL or request.region_code != "CN":
            raise OppoParseError("OPPO official prices require NATIONAL/CN scope")
        categories = set(request.category_codes or self.default_category_codes)
        if request.source_item_codes or not categories.issubset(self.default_category_codes):
            raise OppoParseError("OPPO request includes unsupported selections")
        self._require_origin(self.discovery_url)
        result = await context.http.fetch(
            self.discovery_url, allowed_domains=context.allowed_domains
        )
        self._require_same_url(self.discovery_url, result.request_url)
        self._require_same_url(self.discovery_url, result.final_url)
        if not 200 <= result.status_code < 300:
            raise OppoParseError(f"OPPO discovery returned HTTP {result.status_code}")
        return [
            item
            for item in self.parse_discovery(result.body, result.final_url)
            if item.category_code in categories
        ]

    async def fetch_product(
        self, context: AdapterContext, item: DiscoveredCatalogProduct
    ) -> FetchResult:
        self._validate_product(item)
        if urlsplit(item.url).path == OPPO_DETAIL_PATH:
            first = await context.http.fetch(
                item.url,
                allowed_domains=context.allowed_domains,
            )
            self._require_same_url(item.url, first.request_url)
            self._require_same_url(item.url, first.final_url)
            if first.status_code in {404, 410}:
                return first
            if not 200 <= first.status_code < 300:
                raise OppoParseError(f"OPPO detail returned HTTP {first.status_code}")
            first_payload = self._response_payload(first.body)
            first_data = self._current_data(first_payload)
            sku_ids = self._variant_sku_ids(first_data)
            responses = [first_data]
            evidence = [self._request_evidence(first)]
            fetched_at, duration_ms = first.fetched_at, first.duration_ms
            selected_sku = normalize_text(str(first_data.get("skuId", "")))
            if selected_sku != parse_qs(urlsplit(item.url).query)["skuId"][0]:
                raise OppoParseError("OPPO detail response does not match requested SKU")
            for sku_id in sku_ids:
                if sku_id == selected_sku:
                    continue
                detail_url = self._detail_url(sku_id)
                result = await context.http.fetch(
                    detail_url,
                    allowed_domains=context.allowed_domains,
                )
                self._require_same_url(detail_url, result.request_url)
                self._require_same_url(detail_url, result.final_url)
                if not 200 <= result.status_code < 300:
                    raise OppoParseError(f"OPPO detail returned HTTP {result.status_code}")
                current = self._current_data(self._response_payload(result.body))
                if normalize_text(str(current.get("skuId", ""))) != sku_id:
                    raise OppoParseError("OPPO detail response does not match requested SKU")
                responses.append(current)
                evidence.append(self._request_evidence(result))
                fetched_at = max(fetched_at, result.fetched_at)
                duration_ms += result.duration_ms
            body = json.dumps(
                {
                    "schema": "oppo-oapi-detail-batch-v2",
                    "responses": responses,
                    "requests": evidence,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            return FetchResult(
                request_url=item.url,
                final_url=item.url,
                status_code=first.status_code,
                headers={
                    "content-type": "application/json",
                    "x-device-price-artifact": "oppo-oapi-detail-batch-v2",
                },
                body=body,
                fetched_at=fetched_at,
                duration_ms=duration_ms,
                fetch_method=first.fetch_method,
            )
        return await context.browser.fetch_snapshots(
            item.url, allowed_domains=context.allowed_domains, plan=OPPO_SNAPSHOT_PLAN
        )

    def parse_product(
        self, item: DiscoveredCatalogProduct, result: FetchResult
    ) -> ParsedCatalogProduct:
        self._validate_product(item)
        self._require_same_url(item.url, result.request_url)
        self._require_same_url(item.url, result.final_url)
        if not 200 <= result.status_code < 300:
            raise OppoParseError(f"OPPO product returned HTTP {result.status_code}")
        if urlsplit(result.final_url).path == OPPO_DETAIL_PATH:
            return self._parse_oapi_product(item, result)
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
                raise OppoParseError("OPPO product snapshot lacks name")
            snapshot_name = normalize_text(heading.text())
            name = name or snapshot_name
            if snapshot_name != name:
                raise OppoParseError("OPPO variant snapshots contain different products")
            sku_id = self._sku_id(root)
            if sku_id in seen:
                raise OppoParseError(f"OPPO product repeats SKU: {sku_id}")
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
                    "original_label": "OPPO 商城划线原价" if original else None,
                    "availability": self._availability(root.text()).value,
                }
            )
        if name is None or not skus:
            raise OppoParseError("OPPO snapshot evidence yielded no priced SKU")
        return self._catalog_product(item, name, skus)

    def _parse_oapi_product(
        self,
        item: DiscoveredCatalogProduct,
        result: FetchResult,
    ) -> ParsedCatalogProduct:
        try:
            envelope = json.loads(result.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OppoParseError("OPPO detail evidence is not valid JSON") from error
        responses = envelope.get("responses") if isinstance(envelope, dict) else None
        if (
            not isinstance(envelope, dict)
            or envelope.get("schema") != "oppo-oapi-detail-batch-v2"
            or not isinstance(responses, list)
            or not responses
        ):
            raise OppoParseError("OPPO detail evidence schema is unsupported")

        discovered_name = normalize_text(str(item.metadata.get("discovered_name", "")))
        name: str | None = discovered_name or None
        skus: list[dict[str, object]] = []
        seen: set[str] = set()
        for raw in responses:
            if not isinstance(raw, dict):
                raise OppoParseError("OPPO detail evidence entry is invalid")
            spu_id = normalize_text(str(raw.get("spuId", "")))
            if spu_id != item.external_product_id:
                raise OppoParseError("OPPO detail evidence contains a different product")
            product_name = normalize_text(str(raw.get("seoTitle") or raw.get("product_name") or ""))
            name = name or product_name
            if not product_name or self._category_for_name(product_name) != item.category_code:
                raise OppoParseError("OPPO detail evidence has an unapproved brand or category")
            sku_id = normalize_text(str(raw.get("skuId", "")))
            current_price = raw.get("price")
            if not sku_id.isdigit() or sku_id in seen:
                raise OppoParseError("OPPO detail contains an invalid or duplicate SKU")
            seen.add(sku_id)
            dimensions = self._selected_dimensions(raw)
            color = self._optional_string(raw.get("color") or dimensions.get("颜色"))
            version = self._optional_string(raw.get("config") or dimensions.get("版本"))
            original = self._optional_string(raw.get("original_price"))
            skus.append(
                {
                    "sku_id": sku_id,
                    "version": version,
                    "color": color,
                    "dimensions": dimensions,
                    "current_text": str(current_price) if current_price is not None else None,
                    "original_text": original,
                    "original_label": "OPPO 商城划线原价" if original else None,
                    "availability": self._availability(
                        f"{raw.get('bty_type', '')} {raw.get('product_status', '')}"
                    ).value,
                    "source_url": self._detail_url(sku_id),
                }
            )
        if name is None or not skus:
            raise OppoParseError("OPPO detail evidence yielded no directly priced SKU")
        expected = {sku_id for response in responses for sku_id in self._variant_sku_ids(response)}
        if not expected.issubset(seen):
            raise OppoParseError("OPPO detail evidence omits advertised SKUs")
        return self._catalog_product(item, name, skus)

    def _catalog_product(
        self, item: DiscoveredCatalogProduct, name: str, skus: list[dict[str, object]]
    ) -> ParsedCatalogProduct:
        if self._category_for_name(name) != item.category_code:
            raise OppoParseError("OPPO product is outside the approved brand or categories")
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
        connectivity = attributes.get("网络") or attributes.get("connectivity")
        size = attributes.get("尺寸") or attributes.get("size")
        specification = DeviceSpecification(
            color=color,
            memory=memory,
            capacity=capacity,
            edition=version,
            connectivity=connectivity,
            size=size,
            attributes={
                key: value
                for key, value in attributes.items()
                if key
                not in {"color", "颜色", "version", "版本", "网络", "connectivity", "尺寸", "size"}
            },
        )
        sku_id = normalize_text(str(raw["sku_id"]))
        source_url = self._optional_string(raw.get("source_url")) or item.url
        self._require_product_path(source_url)
        availability = Availability(str(raw["availability"]))
        if raw.get("current_text") is None:
            if availability not in {
                Availability.OFF_SHELF,
                Availability.OUT_OF_STOCK,
                Availability.COMING_SOON,
            }:
                raise OppoParseError("OPPO SKU lacks direct price or explicit unavailable state")
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
                    name="OPPO 中国大陆官方商城",
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
            match = re.fullmatch(r"/cn/web/products/(\d+)\.html", parsed.path)
            if parsed.scheme != "https" or parsed.hostname != "www.opposhop.cn" or match is None:
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
                    url=f"https://www.opposhop.cn/cn/web/products/{product_id}.html",
                    category_code=category,
                    metadata={"discovered_name": name},
                ),
            )
        return list(items.values())

    @classmethod
    def _parse_json_discovery(cls, payload: object) -> list[DiscoveredCatalogProduct]:
        if isinstance(payload, dict) and "code" in payload and payload["code"] != 200:
            raise OppoParseError("OPPO discovery API response is unsuccessful")
        items: dict[str, DiscoveredCatalogProduct] = {}

        def visit(value: object) -> None:
            if isinstance(value, list):
                for child in value:
                    visit(child)
                return
            if not isinstance(value, dict):
                return
            product_id = normalize_text(str(value.get("goodsSpuId", "")))
            sku_id = normalize_text(str(value.get("skuId", "")))
            name = normalize_text(str(value.get("goodsSpuName") or value.get("title") or ""))
            category = cls._category_for_name(name)
            if product_id.isdigit() and sku_id.isdigit() and category is not None:
                items.setdefault(
                    product_id,
                    DiscoveredCatalogProduct(
                        external_product_id=product_id,
                        url=cls._detail_url(sku_id),
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
            raise OppoParseError("OPPO product identity or category is invalid")
        parsed = urlsplit(item.url)
        if parsed.path not in {
            OPPO_DETAIL_PATH,
            f"/cn/web/products/{item.external_product_id}.html",
        }:
            raise OppoParseError("OPPO product URL disagrees with product identity")

    @staticmethod
    def _require_origin(url: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.opposhop.cn"
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
        ):
            raise OppoParseError("OPPO URL is outside the official origin")

    @classmethod
    def _require_same_url(cls, expected: str, actual: str) -> None:
        cls._require_origin(actual)
        first, second = urlsplit(expected), urlsplit(actual)
        if first.path != second.path or parse_qs(first.query) != parse_qs(second.query):
            raise OppoParseError("OPPO response URL differs from its request")

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

    @classmethod
    def _selected_dimensions(cls, raw: dict[str, object]) -> dict[str, str]:
        selected = raw.get("skuAttributes")
        if not isinstance(selected, dict):
            return {}
        labels = cls._attribute_labels(raw.get("attributeList"))
        # Retain every selected official dimension, including fixed bundle/style
        # options. Unknown labels stay as source keys rather than being discarded.
        result: dict[str, str] = {}
        for key, value in selected.items():
            label = labels.get(str(key), str(key))
            if not isinstance(value, (str, int)) or not str(value).strip() or label in result:
                raise OppoParseError("OPPO selected configuration is incomplete or ambiguous")
            result[label] = normalize_text(str(value))
        return result

    @staticmethod
    def _response_payload(body: bytes) -> dict[str, object]:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OppoParseError("OPPO API response is not valid JSON") from error
        if not isinstance(payload, dict) or payload.get("code") != 200:
            raise OppoParseError("OPPO API response is unsuccessful")
        return payload

    @staticmethod
    def _current_data(payload: dict[str, object]) -> dict[str, object]:
        data = payload.get("data")
        current = data.get("_$data") if isinstance(data, dict) else None
        if not isinstance(current, dict):
            raise OppoParseError("OPPO API detail schema is unsupported")
        return current

    @classmethod
    def _variant_sku_ids(cls, current: dict[str, object]) -> list[str]:
        selected = current.get("skuAttributes")
        selected = selected if isinstance(selected, dict) else {}
        labels = cls._attribute_labels(current.get("attributeList"))
        device_keys = {
            key
            for key, label in labels.items()
            if label in {"颜色", "版本", "规格", "尺寸", "网络", "款式"}
        }
        fixed_keys = set(labels) - device_keys
        found: dict[str, None] = {}

        def visit(value: object) -> None:
            if isinstance(value, list):
                for child in value:
                    visit(child)
                return
            if not isinstance(value, dict):
                return
            sku_id = normalize_text(str(value.get("skuId", "")))
            attributes = value.get("attributes")
            if "skuId" in value:
                if not sku_id.isdigit() or not isinstance(attributes, dict):
                    raise OppoParseError(
                        "OPPO advertised SKU identity or specifications are invalid"
                    )
                has_device_spec = not device_keys or all(key in attributes for key in device_keys)
                fixed_match = all(attributes.get(key) == selected.get(key) for key in fixed_keys)
                if fixed_match:
                    if not has_device_spec:
                        raise OppoParseError("OPPO advertised SKU omits required dimensions")
                    found.setdefault(sku_id, None)
            for child in value.values():
                visit(child)

        visit(current.get("attributeList"))
        current_sku = normalize_text(str(current.get("skuId", "")))
        if current_sku.isdigit():
            found.setdefault(current_sku, None)
        if not found or len(found) > 128:
            raise OppoParseError("OPPO SKU count is empty or exceeds the safety limit")
        return list(found)

    @staticmethod
    def _attribute_labels(value: object) -> dict[str, str]:
        labels: dict[str, str] = {}
        if isinstance(value, list):
            for group in value:
                entries = group.get("value") if isinstance(group, dict) else None
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if isinstance(entry, dict):
                        key = normalize_text(str(entry.get("key", "")))
                        label = normalize_text(str(entry.get("_$text1", "")))
                        if key and label:
                            if key in labels and labels[key] != label:
                                raise OppoParseError(
                                    "OPPO specification key has conflicting labels"
                                )
                            labels[key] = label
        return labels

    @staticmethod
    def _detail_url(sku_id: str) -> str:
        query = urlencode(
            {
                "interfaceVersion": "v2",
                "pageCode": "skuDetail",
                "skuId": sku_id,
                "addressId": "",
                "secKillRoundId": "",
                "eventId": "",
            }
        )
        return f"https://www.opposhop.cn{OPPO_DETAIL_PATH}?{query}"

    @staticmethod
    def _category_for_name(name: str) -> str | None:
        value = normalize_text(name)
        upper = value.upper()
        if upper.startswith("ONEPLUS") or "一加" in value:
            return None
        if any(term in value for term in ("权益包", "CARE+", "保障", "耳机", "保护壳", "充电器")):
            return None
        if upper.startswith("OPPO PAD"):
            return "TABLET"
        if upper.startswith("OPPO WATCH"):
            return "WATCH"
        if re.match(r"^OPPO\s+(?:FIND|RENO|A|K)\w*", upper):
            return "PHONE"
        return None

    @classmethod
    def _snapshots(cls, body: bytes, source_url: str) -> list[object]:
        try:
            envelope = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OppoParseError("OPPO snapshot evidence is not valid JSON") from error
        snapshots = envelope.get("snapshots") if isinstance(envelope, dict) else None
        if (
            not isinstance(envelope, dict)
            or envelope.get("schema") != "device-price-browser-snapshots-v1"
            or not isinstance(snapshots, list)
            or not snapshots
        ):
            raise OppoParseError("OPPO snapshot evidence schema is unsupported")
        cls._require_same_url(source_url, str(envelope.get("source_url", "")))
        return snapshots

    @staticmethod
    def _snapshot_fields(snapshot: object) -> tuple[dict[str, object], HtmlNode]:
        if (
            not isinstance(snapshot, dict)
            or not isinstance(snapshot.get("selections"), dict)
            or not isinstance(snapshot.get("html"), str)
        ):
            raise OppoParseError("OPPO snapshot entry is invalid")
        return snapshot["selections"], parse_html(snapshot["html"])

    @staticmethod
    def _sku_id(root: HtmlNode) -> str:
        node = root.first(lambda candidate: bool(candidate.attrs.get("data-sku-id")))
        if node is None:
            raise OppoParseError("OPPO product snapshot has no official SKU ID")
        return normalize_text(node.attrs["data-sku-id"])

    def _resolve_price(
        self, raw: dict[str, object]
    ) -> tuple[Decimal | None, OriginalPriceType, Decimal]:
        original_text = self._optional_string(raw.get("original_text"))
        resolution = self.price_policy.resolve(
            original=PriceCandidate(
                original_text, self._optional_string(raw.get("original_label")) or "OPPO 商城原价"
            )
            if original_text
            else None,
            current=PriceCandidate(str(raw["current_text"]), "OPPO 商城当前售价"),
        )
        original_price = resolution.original_price
        original_type = resolution.original_price_type
        if original_price == resolution.current_price:
            original_price = None
            original_type = OriginalPriceType.NONE
        if resolution.current_price is None:
            raise OppoParseError("OPPO current price is missing")
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
        if "已下架" in text:
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
        legacy = re.fullmatch(r"/cn/web/products/\d+\.html", parsed.path) is not None
        query = parse_qs(parsed.query)
        current = (
            parsed.path == OPPO_DETAIL_PATH
            and query.get("interfaceVersion") == ["v2"]
            and query.get("pageCode") == ["skuDetail"]
            and len(query.get("skuId", [])) == 1
            and query["skuId"][0].isdigit()
        )
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.opposhop.cn"
            or not (legacy or current)
        ):
            raise OppoParseError(f"OPPO URL is outside approved product paths: {url}")

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return normalize_text(str(value)) if value is not None and str(value).strip() else None
