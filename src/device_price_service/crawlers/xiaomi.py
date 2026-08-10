from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from device_price_service.crawlers.base import AdapterContext, BrandAdapter
from device_price_service.crawlers.html import parse_html
from device_price_service.domain.crawl import (
    BrowserFixedOption,
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

XIAOMI_SHOP_URL = "https://www.mi.com/shop/"
XIAOMI_SNAPSHOT_PLAN = BrowserSnapshotPlan(
    ready_selector=".product-con .price-info",
    snapshot_selector=".product-con",
    dimensions=(
        BrowserVariantDimension(
            name="version",
            container_selector=".buy-option .option-box",
            heading_text="选择版本",
        ),
        BrowserVariantDimension(
            name="color",
            container_selector=".buy-option .option-box",
            heading_text="选择颜色",
        ),
    ),
    fixed_options=(BrowserFixedOption(container_selector=".batch-box", value="标准版"),),
    settle_ms=350,
    max_snapshots=96,
)


class XiaomiParseError(ValueError):
    """Raised when a Xiaomi shop fixture no longer exposes required SKU fields."""


class XiaomiAdapter(BrandAdapter):
    brand_code = "XIAOMI"
    channel_code = "XIAOMI_CN_WEB"
    version = "xiaomi-cn-rendered-v1"

    def __init__(self, *, discovery_url: str = XIAOMI_SHOP_URL) -> None:
        self.discovery_url = discovery_url
        self.price_policy = PricePolicy()

    async def discover(self, context: AdapterContext) -> list[DiscoveredProduct]:
        result = await context.http.fetch(
            self.discovery_url,
            allowed_domains=context.allowed_domains,
        )
        if result.status_code < 200 or result.status_code >= 400:
            raise XiaomiParseError(f"Xiaomi discovery returned HTTP {result.status_code}")
        return self.parse_discovery(result.body, result.final_url)

    async def fetch_product(
        self,
        context: AdapterContext,
        item: DiscoveredProduct,
    ) -> FetchResult:
        self._require_shop_path(item.url)
        return await context.browser.fetch_snapshots(
            item.url,
            allowed_domains=context.allowed_domains,
            plan=XIAOMI_SNAPSHOT_PLAN,
        )

    def parse_product(self, item: DiscoveredProduct, result: FetchResult) -> ParsedProduct:
        self._require_shop_path(result.final_url)
        try:
            envelope = json.loads(result.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise XiaomiParseError("Xiaomi snapshot evidence is not valid JSON") from error
        if not isinstance(envelope, dict) or envelope.get("schema") != (
            "device-price-browser-snapshots-v1"
        ):
            raise XiaomiParseError("Xiaomi snapshot evidence schema is unsupported")
        snapshots = envelope.get("snapshots")
        if not isinstance(snapshots, list) or not snapshots:
            raise XiaomiParseError("Xiaomi snapshot evidence contains no variants")

        name: str | None = None
        parsed_skus: list[dict[str, object]] = []
        seen_specs: set[tuple[str, str]] = set()
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                raise XiaomiParseError("Xiaomi snapshot entry is invalid")
            selections = snapshot.get("selections")
            html = snapshot.get("html")
            if not isinstance(selections, dict) or not isinstance(html, str):
                raise XiaomiParseError("Xiaomi snapshot fields are invalid")
            version = normalize_text(str(selections.get("version", "")))
            color = normalize_text(str(selections.get("color", "")))
            if not version or not color:
                raise XiaomiParseError("Xiaomi snapshot is missing version or color")
            key = (version, color)
            if key in seen_specs:
                continue
            seen_specs.add(key)

            root = parse_html(html)
            heading = root.first(lambda node: node.tag == "h2")
            price_box = root.first(lambda node: node.has_class("price-info"))
            if heading is None or price_box is None:
                raise XiaomiParseError("Xiaomi rendered product fragment lacks name or price")
            snapshot_name = normalize_text(heading.text())
            name = name or snapshot_name
            if snapshot_name != name:
                raise XiaomiParseError("Xiaomi variant snapshots contain different products")

            original_node = price_box.first(
                lambda node: node.tag in {"del", "s"}
                or node.has_class("original-price")
                or node.has_class("market-price")
            )
            current_node = price_box.first(lambda node: node.has_class("current-price"))
            if current_node is None:
                current_node = price_box.first(
                    lambda node: node.tag in {"span", "strong"}
                    and not node.has_class("original-price")
                    and not node.has_class("market-price")
                )
            if current_node is None:
                raise XiaomiParseError("Xiaomi variant has no direct current price")

            stock_node = root.first(lambda node: node.has_class("address-box"))
            stock_text = normalize_text(stock_node.text()) if stock_node else ""
            parsed_skus.append(
                {
                    "version": version,
                    "color": color,
                    "current_text": normalize_text(current_node.text()),
                    "original_text": normalize_text(original_node.text())
                    if original_node
                    else None,
                    "original_label": "小米商城划线原价" if original_node else None,
                    "availability": self._availability(stock_text).value,
                }
            )

        if name is None or not parsed_skus:
            raise XiaomiParseError("Xiaomi snapshot evidence yielded no priced SKU")
        return ParsedProduct(
            source_url=result.final_url,
            payload={"name": name, "skus": parsed_skus},
        )

    def normalize(self, item: DiscoveredProduct, parsed: ParsedProduct) -> NormalizedProduct:
        product_name = normalize_text(str(parsed.payload["name"]))
        category_code = item.category_code or self._category_for_name(product_name) or ""
        if category_code not in {"PHONE", "TABLET", "LAPTOP", "WATCH"}:
            raise XiaomiParseError(f"unsupported Xiaomi category: {category_code}")
        if self._category_for_name(product_name) != category_code:
            raise XiaomiParseError("Xiaomi product name no longer matches discovered category")

        raw_skus = parsed.payload.get("skus")
        if not isinstance(raw_skus, list):
            raise XiaomiParseError("Xiaomi parsed SKU payload is invalid")
        normalized_skus: list[NormalizedSku] = []
        for raw in raw_skus:
            if not isinstance(raw, dict):
                raise XiaomiParseError("Xiaomi parsed SKU entry is invalid")
            version = normalize_text(str(raw["version"]))
            color = normalize_text(str(raw["color"]))
            memory, capacity, edition = self._version_attributes(version)
            original_text = self._optional_string(raw.get("original_text"))
            resolution = self.price_policy.resolve(
                original=PriceCandidate(
                    original_text,
                    self._optional_string(raw.get("original_label")) or "小米商城原价",
                )
                if original_text
                else None,
                current=PriceCandidate(str(raw["current_text"]), "小米商城当前售价"),
            )
            original_price = resolution.original_price
            original_type = resolution.original_price_type
            if original_price == resolution.current_price:
                original_price = None
                original_type = OriginalPriceType.NONE
            attributes = {
                "version": version,
                "memory": memory,
                "capacity": capacity,
                "edition": edition,
                "color": color,
                "bundle": "标准版",
            }
            normalized_skus.append(
                NormalizedSku(
                    official_sku_id=None,
                    name=f"{product_name} {version} {color}",
                    color=color,
                    capacity=capacity,
                    memory=memory,
                    attributes=attributes,
                    spec_fingerprint=build_spec_fingerprint(attributes),
                    status="ACTIVE",
                    offers=[
                        NormalizedOffer(
                            official_offer_id=None,
                            source_url=parsed.source_url,
                            original_price=original_price,
                            original_price_type=original_type,
                            current_price=resolution.current_price,
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
            lifecycle_status="ACTIVE",
            skus=normalized_skus,
        )

    @classmethod
    def parse_discovery(cls, body: bytes, page_url: str) -> list[DiscoveredProduct]:
        root = parse_html(body)
        items: dict[str, DiscoveredProduct] = {}
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
                DiscoveredProduct(
                    official_product_id=product_id,
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
        match = re.search(
            r"(?P<memory>\d+\s*GB)\s*\+\s*(?P<capacity>\d+\s*(?:GB|TB))",
            version,
            re.I,
        )
        if match is None:
            return None, None, version
        memory = normalize_capacity(match.group("memory"))
        capacity = normalize_capacity(match.group("capacity"))
        edition = normalize_text(version[match.end() :]) or None
        return memory, capacity, edition

    @staticmethod
    def _availability(value: str) -> Availability:
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
    def _require_shop_path(url: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.mi.com"
            or parsed.path not in {"/shop/buy", "/shop/buy/detail"}
        ):
            raise XiaomiParseError(f"Xiaomi URL is outside approved shop paths: {url}")

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return normalize_text(str(value)) if value is not None and str(value).strip() else None
