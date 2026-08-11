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

HUAWEI_DISCOVERY_URL = (
    "https://openapi.vmall.com/mcp/content/getPageInfoListAsync"
    "?pageId=301&lang=zh_CN&country=CN&portal=2"
)
HUAWEI_ITEM_ORIGIN = "https://item.vmall.com"
HUAWEI_SNAPSHOT_PLAN = BrowserSnapshotPlan(
    ready_selector=".product-meta",
    snapshot_selector=".product-meta",
    dimensions=(
        BrowserVariantDimension(
            name="version",
            container_selector=".product-choose",
            heading_text="版本",
            optional=True,
        ),
        BrowserVariantDimension(
            name="color",
            container_selector=".product-choose",
            heading_text="颜色",
            optional=True,
        ),
    ),
    settle_ms=350,
    max_snapshots=128,
)


class HuaweiParseError(ValueError):
    """Raised when a Huawei Mall fixture no longer exposes required fields."""


class HuaweiAdapter(BrandAdapter):
    brand_code = "HUAWEI"
    channel_code = "HUAWEI_CN_WEB"
    version = "huawei-cn-next-v2"

    def __init__(self, *, discovery_url: str = HUAWEI_DISCOVERY_URL) -> None:
        self.discovery_url = discovery_url
        self.price_policy = PricePolicy()

    async def discover(self, context: AdapterContext) -> list[DiscoveredProduct]:
        result = await context.http.fetch(
            self.discovery_url, allowed_domains=context.allowed_domains
        )
        if result.status_code < 200 or result.status_code >= 400:
            raise HuaweiParseError(f"Huawei discovery returned HTTP {result.status_code}")
        return self.parse_discovery(result.body, result.final_url)

    async def fetch_product(
        self,
        context: AdapterContext,
        item: DiscoveredProduct,
    ) -> FetchResult:
        self._require_product_path(item.url)
        if urlsplit(item.url).hostname == "item.vmall.com":
            return await context.browser.fetch(
                item.url,
                allowed_domains=context.allowed_domains,
            )
        return await context.browser.fetch_snapshots(
            item.url,
            allowed_domains=context.allowed_domains,
            plan=HUAWEI_SNAPSHOT_PLAN,
        )

    def parse_product(self, item: DiscoveredProduct, result: FetchResult) -> ParsedProduct:
        self._require_product_path(result.final_url)
        if urlsplit(result.final_url).hostname == "item.vmall.com":
            return self._parse_next_product(result)
        snapshots = self._snapshots(result.body)
        name: str | None = None
        skus: list[dict[str, object]] = []
        seen: set[str] = set()
        for snapshot in snapshots:
            selections, root = self._snapshot_fields(snapshot)
            heading = root.first(lambda node: node.tag == "h1" or node.has_class("product-name"))
            price = root.first(
                lambda node: node.has_class("current-price") or node.has_class("sale-price")
            )
            if heading is None or price is None:
                raise HuaweiParseError("Huawei product snapshot lacks name or direct price")
            snapshot_name = normalize_text(heading.text())
            name = name or snapshot_name
            if snapshot_name != name:
                raise HuaweiParseError("Huawei variant snapshots contain different products")
            sku_id = self._sku_id(root)
            if sku_id in seen:
                continue
            seen.add(sku_id)
            original = self._original_price(root)
            version = normalize_text(str(selections.get("version", ""))) or None
            color = normalize_text(str(selections.get("color", ""))) or None
            skus.append(
                {
                    "sku_id": sku_id,
                    "version": version,
                    "color": color,
                    "current_text": normalize_text(price.text()),
                    "original_text": normalize_text(original.text()) if original else None,
                    "original_label": "华为商城划线原价" if original else None,
                    "availability": self._availability(root.text()).value,
                }
            )
        if name is None or not skus:
            raise HuaweiParseError("Huawei snapshot evidence yielded no priced SKU")
        return ParsedProduct(source_url=result.final_url, payload={"name": name, "skus": skus})

    @classmethod
    def _parse_next_product(cls, result: FetchResult) -> ParsedProduct:
        match = re.search(
            rb'<script[^>]+id=["\x27]__NEXT_DATA__["\x27][^>]*>(.*?)</script>',
            result.body,
            flags=re.DOTALL,
        )
        if match is None:
            raise HuaweiParseError("Huawei product page lacks __NEXT_DATA__ evidence")
        try:
            envelope = json.loads(match.group(1))
            current = envelope["props"]["pageProps"]["mainData"]["current"]
            options = current["productOptions"]["sbomList"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise HuaweiParseError("Huawei __NEXT_DATA__ product schema is unsupported") from error
        if not isinstance(current, dict) or not isinstance(options, dict):
            raise HuaweiParseError("Huawei __NEXT_DATA__ product schema is unsupported")

        name = normalize_text(str(current.get("name", "")))
        if not name:
            raise HuaweiParseError("Huawei product page lacks a product name")
        skus: list[dict[str, object]] = []
        for raw in options.values():
            if not isinstance(raw, dict):
                continue
            sku_id = normalize_text(str(raw.get("sbomCode", "")))
            price = raw.get("price")
            if not sku_id or price is None:
                continue
            attributes = cls._next_attributes(raw.get("gbomAttrList"))
            version = next(
                (
                    attributes[label]
                    for label in ("配置", "版本", "规格", "容量")
                    if label in attributes
                ),
                None,
            )
            skus.append(
                {
                    "sku_id": sku_id,
                    "version": version,
                    "color": attributes.get("颜色"),
                    "official_attributes": attributes,
                    "current_text": str(price),
                    "original_text": None,
                    "original_label": None,
                    "availability": cls._next_availability(raw).value,
                }
            )
        if not skus:
            raise HuaweiParseError("Huawei __NEXT_DATA__ yielded no directly priced SKU")
        return ParsedProduct(source_url=result.final_url, payload={"name": name, "skus": skus})

    @staticmethod
    def _next_attributes(value: object) -> dict[str, str]:
        if not isinstance(value, list):
            return {}
        attributes: dict[str, str] = {}
        for entry in value:
            if not isinstance(entry, dict):
                continue
            name = normalize_text(str(entry.get("attrName", "")))
            attribute_value = normalize_text(str(entry.get("attrValue", "")))
            if name and attribute_value:
                attributes[name] = attribute_value
        return attributes

    @staticmethod
    def _next_availability(raw: dict[str, object]) -> Availability:
        if raw.get("commingSoonFlag") == 1:
            return Availability.COMING_SOON
        inventory = raw.get("inventory")
        if isinstance(inventory, int) and inventory > 0:
            return Availability.ON_SALE
        # The public page returns zero before a delivery region is selected. Treat that
        # as unknown rather than manufacturing a false out-of-stock observation.
        return Availability.UNKNOWN

    def normalize(self, item: DiscoveredProduct, parsed: ParsedProduct) -> NormalizedProduct:
        product_name = normalize_text(str(parsed.payload["name"]))
        category_code = item.category_code or self._category_for_name(product_name) or ""
        if self._category_for_name(product_name) != category_code:
            raise HuaweiParseError("Huawei product is outside the approved brand or categories")
        raw_skus = parsed.payload.get("skus")
        if not isinstance(raw_skus, list):
            raise HuaweiParseError("Huawei parsed SKU payload is invalid")
        normalized_skus: list[NormalizedSku] = []
        for raw in raw_skus:
            if not isinstance(raw, dict):
                raise HuaweiParseError("Huawei parsed SKU entry is invalid")
            version = self._optional_string(raw.get("version"))
            color = self._optional_string(raw.get("color"))
            official_attributes = raw.get("official_attributes")
            official_attributes = (
                official_attributes if isinstance(official_attributes, dict) else {}
            )
            memory, capacity = self._memory_capacity(version)
            resolution = self._resolve_price(raw, "华为商城")
            attributes = {
                "version": version,
                "color": color,
                "memory": memory,
                "capacity": capacity,
                "official_attributes": official_attributes,
            }
            sku_id = normalize_text(str(raw["sku_id"]))
            official_variant = " ".join(
                dict.fromkeys(
                    normalize_text(str(value))
                    for value in official_attributes.values()
                    if normalize_text(str(value))
                )
            )
            sku_name = " ".join(
                part
                for part in (
                    product_name,
                    official_variant or version,
                    None if official_variant else color,
                )
                if part
            )
            normalized_skus.append(
                NormalizedSku(
                    official_sku_id=sku_id,
                    name=sku_name,
                    color=color,
                    capacity=capacity,
                    memory=memory,
                    attributes=attributes,
                    spec_fingerprint=build_spec_fingerprint(attributes),
                    offers=[
                        NormalizedOffer(
                            official_offer_id=sku_id,
                            source_url=parsed.source_url,
                            original_price=resolution[0],
                            original_price_type=resolution[1],
                            current_price=resolution[2],
                            availability=Availability(str(raw["availability"])),
                        )
                    ],
                )
            )
        return NormalizedProduct(
            brand_code=self.brand_code,
            channel_code=self.channel_code,
            category_code=category_code,
            official_product_id=item.official_product_id,
            name=product_name,
            series_name=product_name,
            official_url=parsed.source_url,
            skus=normalized_skus,
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
            match = re.fullmatch(r"/product/(\d+)\.html", parsed.path)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in {"www.vmall.com", "m.vmall.com"}
                or match is None
            ):
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
                    url=f"https://www.vmall.com/product/{product_id}.html",
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
            product_id = normalize_text(str(value.get("prdId", "")))
            name = normalize_text(str(value.get("prdName") or value.get("briefName") or ""))
            category = cls._category_for_name(name)
            if product_id.isdigit() and category is not None:
                query = {"prdId": product_id}
                sku_code = normalize_text(str(value.get("skuCode", "")))
                if sku_code:
                    query["sbomCode"] = sku_code
                items.setdefault(
                    product_id,
                    DiscoveredProduct(
                        official_product_id=product_id,
                        url=(
                            f"{HUAWEI_ITEM_ORIGIN}/product/comdetail/index.html?{urlencode(query)}"
                        ),
                        category_code=category,
                        metadata={"discovered_name": name},
                    ),
                )
            for child in value.values():
                visit(child)

        visit(payload)
        return list(items.values())

    @staticmethod
    def _category_for_name(name: str) -> str | None:
        value = normalize_text(name)
        upper = value.upper()
        if any(term in upper for term in ("HONOR", "荣耀", "WIKO", "HI NOVA", "华为智选")):
            return None
        if any(
            term in value
            for term in (
                "表带",
                "底座",
                "保护壳",
                "保护套",
                "保护膜",
                "键盘",
                "触控笔",
                "手写笔",
                "充电器",
                "充电线",
                "数据线",
                "扩展坞",
                "鼠标",
                "耳机",
            )
        ):
            return None
        if upper.startswith("HUAWEI MATEPAD") or value.startswith("华为平板"):
            return "TABLET"
        if upper.startswith("HUAWEI MATEBOOK"):
            return "LAPTOP"
        if upper.startswith(("HUAWEI MATESTATION", "HUAWEI 擎云台式")):
            return "DESKTOP"
        if upper.startswith("HUAWEI WATCH") or value.startswith("华为手表"):
            return "WATCH"
        if upper.startswith(("HUAWEI MATE", "HUAWEI PURA", "HUAWEI NOVA", "华为畅享")):
            return "PHONE"
        return None

    @staticmethod
    def _snapshots(body: bytes) -> list[object]:
        try:
            envelope = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HuaweiParseError("Huawei snapshot evidence is not valid JSON") from error
        snapshots = envelope.get("snapshots") if isinstance(envelope, dict) else None
        if (
            not isinstance(envelope, dict)
            or envelope.get("schema") != "device-price-browser-snapshots-v1"
            or not isinstance(snapshots, list)
            or not snapshots
        ):
            raise HuaweiParseError("Huawei snapshot evidence schema is unsupported")
        return snapshots

    @staticmethod
    def _snapshot_fields(snapshot: object) -> tuple[dict[str, object], HtmlNode]:
        if (
            not isinstance(snapshot, dict)
            or not isinstance(snapshot.get("selections"), dict)
            or not isinstance(snapshot.get("html"), str)
        ):
            raise HuaweiParseError("Huawei snapshot entry is invalid")
        return snapshot["selections"], parse_html(snapshot["html"])

    @staticmethod
    def _sku_id(root: HtmlNode) -> str:
        node = root.first(lambda candidate: bool(candidate.attrs.get("data-sku-id")))
        if node is None:
            raise HuaweiParseError("Huawei product snapshot has no official SKU ID")
        return normalize_text(node.attrs["data-sku-id"])

    @staticmethod
    def _original_price(root: HtmlNode) -> HtmlNode | None:
        return root.first(
            lambda node: (
                node.tag in {"del", "s"}
                or node.has_class("original-price")
                or node.has_class("market-price")
            )
        )

    @staticmethod
    def _availability(text: str) -> Availability:
        if any(term in text for term in ("暂时缺货", "无货", "到货通知")):
            return Availability.OUT_OF_STOCK
        if "预约" in text:
            return Availability.RESERVATION
        if "预售" in text:
            return Availability.PRE_SALE
        if any(term in text for term in ("立即购买", "加入购物车", "现货")):
            return Availability.ON_SALE
        return Availability.UNKNOWN

    def _resolve_price(
        self, raw: dict[str, object], label: str
    ) -> tuple[Decimal | None, OriginalPriceType, Decimal]:
        original_text = self._optional_string(raw.get("original_text"))
        resolution = self.price_policy.resolve(
            original=PriceCandidate(
                original_text, self._optional_string(raw.get("original_label")) or f"{label}原价"
            )
            if original_text
            else None,
            current=PriceCandidate(str(raw["current_text"]), f"{label}当前售价"),
        )
        original_price = resolution.original_price
        original_type = resolution.original_price_type
        if original_price == resolution.current_price:
            original_price = None
            original_type = OriginalPriceType.NONE
        if resolution.current_price is None:
            raise HuaweiParseError("Huawei current price is missing")
        return original_price, original_type, resolution.current_price

    @staticmethod
    def _memory_capacity(version: str | None) -> tuple[str | None, str | None]:
        if not version:
            return None, None
        match = re.search(r"(\d+\s*GB)\s*(?:\+|/)\s*(\d+\s*(?:GB|TB))", version, re.I)
        return (
            (normalize_capacity(match.group(1)), normalize_capacity(match.group(2)))
            if match
            else (None, None)
        )

    @staticmethod
    def _require_product_path(url: str) -> None:
        parsed = urlsplit(url)
        legacy_path = (
            parsed.hostname in {"www.vmall.com", "m.vmall.com"}
            and re.fullmatch(r"/product/\d+\.html", parsed.path) is not None
        )
        item_query = parse_qs(parsed.query)
        current_path = (
            parsed.hostname == "item.vmall.com"
            and parsed.path == "/product/comdetail/index.html"
            and len(item_query.get("prdId", [])) == 1
            and item_query["prdId"][0].isdigit()
        )
        if parsed.scheme != "https" or not (legacy_path or current_path):
            raise HuaweiParseError(f"Huawei URL is outside approved product paths: {url}")

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return normalize_text(str(value)) if value is not None and str(value).strip() else None
