from __future__ import annotations

import json
import re
from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from device_price_service.crawlers.base import AdapterContext, BrandAdapter
from device_price_service.crawlers.html import HtmlNode, parse_html
from device_price_service.domain.crawl import (
    BrowserSnapshotPlan,
    BrowserVariantDimension,
    DiscoveredProduct,
    FetchResult,
    NormalizedOffer,
    NormalizedProduct,
    NormalizedSku,
    ParsedProduct,
)
from device_price_service.domain.enums import Availability, OriginalPriceType
from device_price_service.domain.price_policy import PriceCandidate, PricePolicy
from device_price_service.normalization.specs import (
    build_spec_fingerprint,
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


class OppoAdapter(BrandAdapter):
    brand_code = "OPPO"
    channel_code = "OPPO_CN_WEB"
    version = "oppo-cn-oapi-v2"

    def __init__(self, *, discovery_url: str = OPPO_DISCOVERY_URL) -> None:
        self.discovery_url = discovery_url
        self.price_policy = PricePolicy()

    async def discover(self, context: AdapterContext) -> list[DiscoveredProduct]:
        result = await context.http.fetch(
            self.discovery_url, allowed_domains=context.allowed_domains
        )
        if not 200 <= result.status_code < 400:
            raise OppoParseError(f"OPPO discovery returned HTTP {result.status_code}")
        return self.parse_discovery(result.body, result.final_url)

    async def fetch_product(self, context: AdapterContext, item: DiscoveredProduct) -> FetchResult:
        self._require_product_path(item.url)
        if urlsplit(item.url).path == OPPO_DETAIL_PATH:
            first = await context.http.fetch(
                item.url,
                allowed_domains=context.allowed_domains,
            )
            first_payload = self._response_payload(first.body)
            first_data = self._current_data(first_payload)
            sku_ids = self._variant_sku_ids(first_data)
            responses = [first_data]
            selected_sku = normalize_text(str(first_data.get("skuId", "")))
            for sku_id in sku_ids:
                if sku_id == selected_sku:
                    continue
                result = await context.http.fetch(
                    self._detail_url(sku_id),
                    allowed_domains=context.allowed_domains,
                )
                if not 200 <= result.status_code < 400:
                    raise OppoParseError(f"OPPO detail returned HTTP {result.status_code}")
                responses.append(self._current_data(self._response_payload(result.body)))
            body = json.dumps(
                {"schema": "oppo-oapi-detail-batch-v2", "responses": responses},
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
                fetched_at=first.fetched_at,
                duration_ms=first.duration_ms,
                fetch_method=first.fetch_method,
            )
        return await context.browser.fetch_snapshots(
            item.url, allowed_domains=context.allowed_domains, plan=OPPO_SNAPSHOT_PLAN
        )

    def parse_product(self, item: DiscoveredProduct, result: FetchResult) -> ParsedProduct:
        self._require_product_path(result.final_url)
        if urlsplit(result.final_url).path == OPPO_DETAIL_PATH:
            return self._parse_oapi_product(item, result)
        name: str | None = None
        skus: list[dict[str, object]] = []
        seen: set[str] = set()
        for snapshot in self._snapshots(result.body):
            selections, root = self._snapshot_fields(snapshot)
            heading = root.first(lambda node: node.tag == "h1" or node.has_class("product-name"))
            current = root.first(
                lambda node: node.has_class("current-price") or node.has_class("sale-price")
            )
            if heading is None or current is None:
                raise OppoParseError("OPPO product snapshot lacks name or direct price")
            snapshot_name = normalize_text(heading.text())
            name = name or snapshot_name
            if snapshot_name != name:
                raise OppoParseError("OPPO variant snapshots contain different products")
            sku_id = self._sku_id(root)
            if sku_id in seen:
                continue
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
                    "current_text": normalize_text(current.text()),
                    "original_text": normalize_text(original.text()) if original else None,
                    "original_label": "OPPO 商城划线原价" if original else None,
                    "availability": self._availability(root.text()).value,
                }
            )
        if name is None or not skus:
            raise OppoParseError("OPPO snapshot evidence yielded no priced SKU")
        return ParsedProduct(source_url=result.final_url, payload={"name": name, "skus": skus})

    @classmethod
    def _parse_oapi_product(
        cls,
        item: DiscoveredProduct,
        result: FetchResult,
    ) -> ParsedProduct:
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
            if spu_id != item.official_product_id:
                raise OppoParseError("OPPO detail evidence contains a different product")
            product_name = normalize_text(str(raw.get("seoTitle") or raw.get("product_name") or ""))
            name = name or product_name
            if not product_name:
                raise OppoParseError("OPPO detail evidence contains an empty product name")
            sku_id = normalize_text(str(raw.get("skuId", "")))
            current_price = raw.get("price")
            if not sku_id or current_price is None or sku_id in seen:
                continue
            seen.add(sku_id)
            attributes = raw.get("skuAttributes")
            attributes = attributes if isinstance(attributes, dict) else {}
            color = cls._optional_string(raw.get("color") or attributes.get("key1"))
            version = cls._optional_string(raw.get("config") or attributes.get("key2"))
            original = cls._optional_string(raw.get("original_price"))
            skus.append(
                {
                    "sku_id": sku_id,
                    "version": version,
                    "color": color,
                    "current_text": str(current_price),
                    "original_text": original,
                    "original_label": "OPPO 商城划线原价" if original else None,
                    "availability": cls._availability(
                        f"{raw.get('bty_type', '')} {raw.get('product_status', '')}"
                    ).value,
                    "source_url": cls._detail_url(sku_id),
                }
            )
        if name is None or not skus:
            raise OppoParseError("OPPO detail evidence yielded no directly priced SKU")
        return ParsedProduct(source_url=result.final_url, payload={"name": name, "skus": skus})

    def normalize(self, item: DiscoveredProduct, parsed: ParsedProduct) -> NormalizedProduct:
        name = normalize_text(str(parsed.payload["name"]))
        category = item.category_code or self._category_for_name(name) or ""
        if self._category_for_name(name) != category:
            raise OppoParseError("OPPO product is outside the approved brand or categories")
        raw_skus = parsed.payload.get("skus")
        if not isinstance(raw_skus, list):
            raise OppoParseError("OPPO parsed SKU payload is invalid")
        skus: list[NormalizedSku] = []
        for raw in raw_skus:
            if not isinstance(raw, dict):
                raise OppoParseError("OPPO parsed SKU entry is invalid")
            version = self._optional_string(raw.get("version"))
            color = self._optional_string(raw.get("color"))
            memory, capacity = self._memory_capacity(version)
            original_price, original_type, current_price = self._resolve_price(raw)
            attributes = {
                "version": version,
                "color": color,
                "memory": memory,
                "capacity": capacity,
            }
            sku_id = normalize_text(str(raw["sku_id"]))
            skus.append(
                NormalizedSku(
                    official_sku_id=sku_id,
                    name=" ".join(part for part in (name, version, color) if part),
                    color=color,
                    capacity=capacity,
                    memory=memory,
                    attributes=attributes,
                    spec_fingerprint=build_spec_fingerprint(attributes),
                    offers=[
                        NormalizedOffer(
                            official_offer_id=sku_id,
                            source_url=self._optional_string(raw.get("source_url"))
                            or parsed.source_url,
                            original_price=original_price,
                            original_price_type=original_type,
                            current_price=current_price,
                            availability=Availability(str(raw["availability"])),
                        )
                    ],
                )
            )
        return NormalizedProduct(
            brand_code=self.brand_code,
            channel_code=self.channel_code,
            category_code=category,
            official_product_id=item.official_product_id,
            name=name,
            series_name=name,
            official_url=parsed.source_url,
            skus=skus,
        )

    @classmethod
    def parse_discovery(cls, body: bytes, page_url: str) -> list[DiscoveredProduct]:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, (dict, list)):
            return cls._parse_json_discovery(payload)

        root = parse_html(body)
        items: dict[str, DiscoveredProduct] = {}
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
                DiscoveredProduct(
                    official_product_id=product_id,
                    url=f"https://www.opposhop.cn/cn/web/products/{product_id}.html",
                    category_code=category,
                    metadata={"discovered_name": name},
                ),
            )
        return list(items.values())

    @classmethod
    def _parse_json_discovery(cls, payload: object) -> list[DiscoveredProduct]:
        items: dict[str, DiscoveredProduct] = {}

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
                    DiscoveredProduct(
                        official_product_id=product_id,
                        url=cls._detail_url(sku_id),
                        category_code=category,
                        metadata={"discovered_name": name, "seed_sku_id": sku_id},
                    ),
                )
            for child in value.values():
                visit(child)

        visit(payload)
        return list(items.values())

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
        device_keys, fixed_keys = cls._attribute_keys(current.get("attributeList"))
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
            if sku_id.isdigit() and isinstance(attributes, dict):
                has_device_spec = not device_keys or all(key in attributes for key in device_keys)
                fixed_match = all(attributes.get(key) == selected.get(key) for key in fixed_keys)
                if has_device_spec and fixed_match:
                    found.setdefault(sku_id, None)
            for child in value.values():
                visit(child)

        visit(current.get("attributeList"))
        current_sku = normalize_text(str(current.get("skuId", "")))
        if current_sku.isdigit():
            found.setdefault(current_sku, None)
        return list(found)

    @staticmethod
    def _attribute_keys(value: object) -> tuple[set[str], set[str]]:
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
                            labels[key] = label
        device_keys = {
            key for key, label in labels.items() if label in {"颜色", "版本", "规格", "尺寸"}
        }
        return device_keys, set(labels) - device_keys

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

    @staticmethod
    def _snapshots(body: bytes) -> list[object]:
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

    @staticmethod
    def _require_product_path(url: str) -> None:
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
