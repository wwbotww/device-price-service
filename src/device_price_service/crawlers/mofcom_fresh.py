from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

from device_price_service.crawlers.base import AdapterContext
from device_price_service.crawlers.catalog import CatalogDatasetConnector
from device_price_service.crawlers.html import HtmlNode, parse_html
from device_price_service.db.catalog_seed import (
    MOFCOM_FRESH_CHANNEL_CODE,
    MOFCOM_FRESH_CONNECTOR_CODE,
)
from device_price_service.domain.catalog_crawl import (
    CatalogCollectionRequest,
    CatalogRegion,
    DiscoveredCatalogDataset,
    DiscoveredCatalogListing,
    ParsedCatalogDataset,
    ParsedCatalogListing,
    ParsedCatalogRow,
    SourceMerchant,
    SourcePriceCandidate,
)
from device_price_service.domain.catalog_enums import (
    Availability,
    CollectionFetchMethod,
    FeeStatus,
    PriceNature,
    PriceType,
    PricingBasis,
    RegionScope,
    SellerType,
    VerificationStatus,
)
from device_price_service.domain.crawl import FetchResult


class MofcomFreshSourceError(ValueError):
    """Raised when a MOFCOM price page cannot be mapped without guessing."""

    def __init__(self, message: str, *, error_code: str = "MOFCOM_SOURCE_INVALID") -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class MofcomCommodityMapping:
    commodity_code: str
    commodity_id: str
    source_name: str
    source_group: str


@dataclass(slots=True)
class _MarketRows:
    market_id: str
    market_name: str
    prices: list[tuple[CatalogRegion, Decimal, str]] = field(default_factory=list)


MOFCOM_FRESH_CATEGORY_CODE = "FRESH_MONITORED_COMMODITY"
MOFCOM_FRESH_BASE_URL = "https://cif.mofcom.gov.cn/cif/seach.fhtml"
MOFCOM_FRESH_ALLOWED_DOMAINS = ("cif.mofcom.gov.cn",)

MOFCOM_SUPPORTED_COMMODITIES = (
    MofcomCommodityMapping("NAPA_CABBAGE", "170060", "大白菜", "蔬菜"),
    MofcomCommodityMapping("CUCUMBER", "170130", "黄瓜", "蔬菜"),
    MofcomCommodityMapping("CARROT", "170260", "胡萝卜", "蔬菜"),
    MofcomCommodityMapping("WHITE_RADISH", "170070", "白萝卜", "蔬菜"),
    MofcomCommodityMapping("TOMATO", "170120", "西红柿", "蔬菜"),
    MofcomCommodityMapping("POTATO", "170080", "土豆", "蔬菜"),
    MofcomCommodityMapping("ONION", "170090", "洋葱", "蔬菜"),
    MofcomCommodityMapping("GARLIC", "170100", "蒜头", "蔬菜"),
    MofcomCommodityMapping("GINGER", "170110", "生姜", "蔬菜"),
    MofcomCommodityMapping("EGGPLANT", "170140", "茄子", "蔬菜"),
    MofcomCommodityMapping("BROCCOLI", "170340", "西兰花", "蔬菜"),
    MofcomCommodityMapping("CHICKEN_EGG", "150010", "鲜鸡蛋", "禽蛋"),
    MofcomCommodityMapping("PORK_HIND", "130014", "后臀尖", "肉类"),
    MofcomCommodityMapping("BEEF_LEG", "130025", "牛腿肉", "肉类"),
    MofcomCommodityMapping("DRESSED_CHICKEN", "280020", "白条鸡", "肉禽蛋"),
)

_MAPPING_BY_CODE = {
    item.commodity_code: item for item in MOFCOM_SUPPORTED_COMMODITIES
}
_PAGE_TITLE_PATTERN = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"(?P<commodity>.+?)批发价格行情[（(]单位：(?P<unit>[^）)]+)[）)]"
)
_PRICE_PATTERN = re.compile(r"(?:0|[1-9]\d*)(?:\.\d{1,2})?")
_TABLE_HEADERS = ("地区", "市场", "当日价格", "前一日价格", "环比", "走势图")
_SOURCE_UNIT = "元/公斤"
_SOURCE_SPECIFICATION = "批发市场当日报价"
_CHINA_TIME_ZONE = ZoneInfo("Asia/Shanghai")

# Province-level GB/T 2260 codes are used where one exists. The Xinjiang Production
# and Construction Corps is explicitly named by the source but is not a province;
# XJ_CORPS is therefore retained as a source-scoped region code instead of guessing.
MOFCOM_REGION_CODES = {
    "北京市": "110000",
    "天津市": "120000",
    "河北省": "130000",
    "山西省": "140000",
    "内蒙古自治区": "150000",
    "辽宁省": "210000",
    "吉林省": "220000",
    "黑龙江省": "230000",
    "上海市": "310000",
    "江苏省": "320000",
    "浙江省": "330000",
    "安徽省": "340000",
    "福建省": "350000",
    "江西省": "360000",
    "山东省": "370000",
    "河南省": "410000",
    "湖北省": "420000",
    "湖南省": "430000",
    "广东省": "440000",
    "广西壮族自治区": "450000",
    "海南省": "460000",
    "重庆市": "500000",
    "四川省": "510000",
    "贵州省": "520000",
    "云南省": "530000",
    "西藏自治区": "540000",
    "陕西省": "610000",
    "甘肃省": "620000",
    "青海省": "630000",
    "宁夏回族自治区": "640000",
    "新疆维吾尔自治区": "650000",
    "新疆生产建设兵团": "XJ_CORPS",
}


class MofcomFreshWholesaleConnector(CatalogDatasetConnector):
    """MOFCOM "百家日报" market-level wholesale-price connector."""

    channel_code = MOFCOM_FRESH_CHANNEL_CODE
    connector_code = MOFCOM_FRESH_CONNECTOR_CODE
    version = "mofcom-fresh-wholesale-1"
    fetch_method = CollectionFetchMethod.HTTP
    allowed_domains = MOFCOM_FRESH_ALLOWED_DOMAINS

    async def discover_dataset(
        self,
        context: AdapterContext,
        request: CatalogCollectionRequest,
    ) -> DiscoveredCatalogDataset:
        mapping = self._mapping_for_request(request)
        page_url = _commodity_url(mapping.commodity_id)
        discovery = await context.http.fetch(
            page_url,
            allowed_domains=context.allowed_domains,
            tls_profile="TLS12_COMPAT",
        )
        self._require_html_success(discovery, label="discovery")
        self._validate_page_url(discovery.final_url, mapping)
        source_date = self._parse_page_identity(discovery.body, mapping)
        source_observed_at = _source_day_to_utc(source_date)
        self._require_not_future(source_observed_at, discovery.fetched_at)
        return DiscoveredCatalogDataset(
            dataset_key=f"mofcom-bj:{mapping.commodity_id}:{source_date.isoformat()}",
            url=page_url,
            source_page_url=discovery.final_url,
            source_observed_at=source_observed_at,
            metadata={
                "source_date": source_date.isoformat(),
                "time_precision": "DAY",
                "commodity_code": mapping.commodity_code,
                "commodity_id": mapping.commodity_id,
                "commodity_name": mapping.source_name,
                "discovery_hash": discovery.source_hash,
            },
        )

    async def fetch_dataset(
        self,
        context: AdapterContext,
        dataset: DiscoveredCatalogDataset,
    ) -> FetchResult:
        return await context.http.fetch(
            dataset.url,
            allowed_domains=context.allowed_domains,
            tls_profile="TLS12_COMPAT",
        )

    def parse_dataset(
        self,
        dataset: DiscoveredCatalogDataset,
        result: FetchResult,
    ) -> ParsedCatalogDataset:
        self._require_html_success(result, label="dataset")
        mapping = _mapping_for_dataset(dataset)
        self._validate_page_url(result.final_url, mapping)
        source_date = self._parse_page_identity(result.body, mapping)
        expected_date = _dataset_source_date(dataset)
        if source_date != expected_date:
            raise MofcomFreshSourceError(
                "MOFCOM dataset date changed between discovery and fetch",
                error_code="SOURCE_DATE_MISMATCH",
            )
        self._require_not_future(dataset.source_observed_at, result.fetched_at)
        return self.parse_table(
            dataset,
            root=parse_html(result.body),
            mapping=mapping,
        )

    @staticmethod
    def parse_table(
        dataset: DiscoveredCatalogDataset,
        *,
        root: HtmlNode,
        mapping: MofcomCommodityMapping,
    ) -> ParsedCatalogDataset:
        tables = root.find_all(
            lambda node: node.tag == "table" and node.attrs.get("id") == "goaler"
        )
        if len(tables) != 1:
            raise MofcomFreshSourceError(
                f"expected one MOFCOM price table, found {len(tables)}",
                error_code="DATASET_STRUCTURE_CHANGED",
            )
        table_rows = tables[0].find_all(lambda node: node.tag == "tr")
        if len(table_rows) < 2:
            raise MofcomFreshSourceError(
                "MOFCOM price table contains no market rows",
                error_code="DATASET_EMPTY",
            )
        headers = tuple(_compact_text(cell.text()) for cell in _row_cells(table_rows[0]))
        if headers != _TABLE_HEADERS:
            raise MofcomFreshSourceError(
                f"unexpected MOFCOM table headers: {headers!r}",
                error_code="DATASET_STRUCTURE_CHANGED",
            )

        source_date = _dataset_source_date(dataset)
        by_market: dict[str, _MarketRows] = {}
        seen_market_regions: set[tuple[str, str]] = set()
        for row_number, row in enumerate(table_rows[1:], start=2):
            cells = _row_cells(row)
            if len(cells) != len(_TABLE_HEADERS):
                raise MofcomFreshSourceError(
                    f"MOFCOM table row {row_number} has {len(cells)} cells",
                    error_code="DATASET_STRUCTURE_CHANGED",
                )
            region_name, market_name, current_text = (
                _compact_text(cell.text()) for cell in cells[:3]
            )
            region = _source_region(region_name)
            if not market_name:
                raise MofcomFreshSourceError(
                    f"MOFCOM table row {row_number} has no market name",
                    error_code="MARKET_IDENTITY_INVALID",
                )
            current_price = _positive_price(current_text, row_number=row_number)
            market_id = _market_id_from_chart(
                cells[5],
                commodity_id=mapping.commodity_id,
                source_date=source_date,
                page_url=dataset.source_page_url,
                row_number=row_number,
            )
            market_region_key = (market_id, region.code)
            if market_region_key in seen_market_regions:
                raise MofcomFreshSourceError(
                    f"duplicate MOFCOM market/region row: {market_id}/{region.code}",
                    error_code="DATASET_DUPLICATE_ROW",
                )
            seen_market_regions.add(market_region_key)
            market = by_market.setdefault(
                market_id,
                _MarketRows(market_id=market_id, market_name=market_name),
            )
            if market.market_name != market_name:
                raise MofcomFreshSourceError(
                    f"MOFCOM market {market_id} has conflicting names",
                    error_code="MARKET_IDENTITY_INVALID",
                )
            market.prices.append((region, current_price, current_text))

        parsed_rows = [
            _parsed_market_row(dataset, mapping=mapping, market=market)
            for market in sorted(by_market.values(), key=lambda item: int(item.market_id))
        ]
        return ParsedCatalogDataset(rows=parsed_rows)

    @staticmethod
    def _mapping_for_request(request: CatalogCollectionRequest) -> MofcomCommodityMapping:
        if request.region_scope is not RegionScope.MULTI or request.region_code != "*":
            raise MofcomFreshSourceError(
                "MOFCOM wholesale connector requires MULTI/* scope",
                error_code="SCOPE_UNSUPPORTED",
            )
        if request.category_codes and MOFCOM_FRESH_CATEGORY_CODE not in request.category_codes:
            raise MofcomFreshSourceError(
                f"MOFCOM connector requires {MOFCOM_FRESH_CATEGORY_CODE}",
                error_code="SCOPE_UNSUPPORTED",
            )
        if len(request.source_item_codes) != 1:
            raise MofcomFreshSourceError(
                "MOFCOM connector requires exactly one source_item_code",
                error_code="SOURCE_ITEM_REQUIRED",
            )
        code = request.source_item_codes[0]
        try:
            return _MAPPING_BY_CODE[code]
        except KeyError as error:
            raise MofcomFreshSourceError(
                f"unsupported MOFCOM commodity code: {code}",
                error_code="SOURCE_ITEM_UNSUPPORTED",
            ) from error

    @staticmethod
    def _parse_page_identity(body: bytes, mapping: MofcomCommodityMapping) -> date:
        root = parse_html(body)
        title_nodes = root.find_all(
            lambda node: node.tag == "div" and node.has_class("secondbiaotitle")
        )
        if len(title_nodes) != 1:
            raise MofcomFreshSourceError(
                f"expected one MOFCOM price title, found {len(title_nodes)}",
                error_code="DISCOVERY_STRUCTURE_CHANGED",
            )
        title = _compact_text(title_nodes[0].text())
        match = _PAGE_TITLE_PATTERN.fullmatch(title)
        if match is None:
            raise MofcomFreshSourceError(
                f"unrecognized MOFCOM price title: {title!r}",
                error_code="DISCOVERY_STRUCTURE_CHANGED",
            )
        if match.group("commodity") != mapping.source_name or match.group("unit") != _SOURCE_UNIT:
            raise MofcomFreshSourceError(
                "MOFCOM page commodity or unit does not match its configured mapping",
                error_code="COMMODITY_MAPPING_MISSING",
            )

        navigation_matches = []
        for anchor in root.find_all(lambda node: node.tag == "a"):
            if _compact_text(anchor.text()) != mapping.source_name:
                continue
            href = anchor.attrs.get("href", "").strip()
            query = parse_qs(urlsplit(urljoin(MOFCOM_FRESH_BASE_URL, href)).query)
            if query.get("commdityid") == [mapping.commodity_id]:
                navigation_matches.append(anchor)
        if len(navigation_matches) != 1:
            raise MofcomFreshSourceError(
                "MOFCOM commodity navigation no longer matches the configured ID",
                error_code="COMMODITY_MAPPING_MISSING",
            )
        return _date_from_match(match)

    @staticmethod
    def _validate_page_url(url: str, mapping: MofcomCommodityMapping) -> None:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if (
            parsed.hostname != "cif.mofcom.gov.cn"
            or parsed.path != "/cif/seach.fhtml"
            or query.get("commdityid") != [mapping.commodity_id]
        ):
            raise MofcomFreshSourceError(
                "MOFCOM page URL does not match the approved commodity endpoint",
                error_code="SOURCE_URL_INVALID",
            )

    @staticmethod
    def _require_not_future(source_observed_at: datetime, fetched_at: datetime) -> None:
        if source_observed_at > fetched_at + timedelta(minutes=5):
            raise MofcomFreshSourceError(
                "MOFCOM source date is later than fetch time",
                error_code="SOURCE_DATE_MISMATCH",
            )

    @staticmethod
    def _require_html_success(result: FetchResult, *, label: str) -> None:
        if not 200 <= result.status_code < 400:
            raise MofcomFreshSourceError(
                f"unexpected MOFCOM {label} HTTP status: {result.status_code}"
            )
        if result.content_type not in {"text/html", "application/xhtml+xml", ""}:
            raise MofcomFreshSourceError(
                f"unexpected MOFCOM {label} content type: {result.content_type!r}"
            )


def _parsed_market_row(
    dataset: DiscoveredCatalogDataset,
    *,
    mapping: MofcomCommodityMapping,
    market: _MarketRows,
) -> ParsedCatalogRow:
    item = DiscoveredCatalogListing(
        listing_key=(
            f"mofcom-bj:{mapping.commodity_id}:market:{market.market_id}:unit:kg"
        ),
        url=dataset.source_page_url,
        category_code=MOFCOM_FRESH_CATEGORY_CODE,
        merchant=SourceMerchant(
            merchant_key=f"mofcom-bj-market:{market.market_id}",
            name=market.market_name,
            seller_type=SellerType.PUBLIC_MARKET,
            verification_status=VerificationStatus.VERIFIED,
            external_merchant_id=market.market_id,
        ),
        price_nature=PriceNature.WHOLESALE_AVERAGE,
        external_product_id=mapping.commodity_code,
        external_sku_id=f"{mapping.commodity_code}:{market.market_id}",
        metadata={
            "commodity_id": mapping.commodity_id,
            "market_id": market.market_id,
        },
    )
    parsed = ParsedCatalogListing(
        source_title=f"{mapping.source_name} - {market.market_name} - {_SOURCE_UNIT}",
        source_category_path=f"生鲜食品/{mapping.source_group}/{mapping.source_name}",
        source_attributes={
            "commodity_code": mapping.commodity_code,
            "commodity_name": mapping.source_name,
            "commodity_group": mapping.source_group,
            "source_specification": _SOURCE_SPECIFICATION,
            "quoted_unit": _SOURCE_UNIT,
            "net_content": "1公斤",
            "source_commodity_id": mapping.commodity_id,
            "source_market_id": market.market_id,
            "source_market_name": market.market_name,
            "time_precision": "DAY",
        },
        price_candidates=[
            SourcePriceCandidate(
                current_price=price,
                price_type=PriceType.PUBLISHED_VALUE,
                pricing_basis=PricingBasis.UNIT_QUOTED,
                availability=Availability.UNKNOWN,
                fee_status=FeeStatus.NOT_APPLICABLE,
                displayed_price_text=f"{displayed_price}元/公斤",
                evidence_hash=_price_row_evidence_hash(
                    "MOFCOM_FRESH_WHOLESALE",
                    _dataset_source_date(dataset).isoformat(),
                    mapping.commodity_id,
                    mapping.source_name,
                    _SOURCE_UNIT,
                    market.market_id,
                    market.market_name,
                    region.code,
                    displayed_price,
                ),
                region=region,
                source_observed_at=dataset.source_observed_at,
            )
            for region, price, displayed_price in sorted(
                market.prices,
                key=lambda item: item[0].code,
            )
        ],
    )
    return ParsedCatalogRow(item=item, parsed=parsed)


def _mapping_for_dataset(dataset: DiscoveredCatalogDataset) -> MofcomCommodityMapping:
    code = dataset.metadata.get("commodity_code")
    commodity_id = dataset.metadata.get("commodity_id")
    if not isinstance(code, str) or not isinstance(commodity_id, str):
        raise MofcomFreshSourceError(
            "MOFCOM dataset is missing commodity metadata",
            error_code="COMMODITY_MAPPING_MISSING",
        )
    mapping = _MAPPING_BY_CODE.get(code)
    if mapping is None or mapping.commodity_id != commodity_id:
        raise MofcomFreshSourceError(
            "MOFCOM dataset commodity metadata is inconsistent",
            error_code="COMMODITY_MAPPING_MISSING",
        )
    return mapping


def _dataset_source_date(dataset: DiscoveredCatalogDataset) -> date:
    raw = dataset.metadata.get("source_date")
    if not isinstance(raw, str):
        raise MofcomFreshSourceError(
            "MOFCOM dataset is missing source_date metadata",
            error_code="SOURCE_DATE_INVALID",
        )
    try:
        source_date = date.fromisoformat(raw)
    except ValueError as error:
        raise MofcomFreshSourceError(
            "MOFCOM source_date metadata is invalid",
            error_code="SOURCE_DATE_INVALID",
        ) from error
    if dataset.source_observed_at != _source_day_to_utc(source_date):
        raise MofcomFreshSourceError(
            "MOFCOM source date and observation time disagree",
            error_code="SOURCE_DATE_MISMATCH",
        )
    return source_date


def _commodity_url(commodity_id: str) -> str:
    return f"{MOFCOM_FRESH_BASE_URL}?{urlencode({'commdityid': commodity_id})}"


def _row_cells(row: HtmlNode) -> list[HtmlNode]:
    return [child for child in row.children if child.tag in {"td", "th"}]


def _source_region(source_name: str) -> CatalogRegion:
    try:
        code = MOFCOM_REGION_CODES[source_name]
    except KeyError as error:
        raise MofcomFreshSourceError(
            f"unsupported MOFCOM region: {source_name!r}",
            error_code="REGION_MAPPING_MISSING",
        ) from error
    return CatalogRegion(scope=RegionScope.PROVINCE, code=code)


def _positive_price(value: str, *, row_number: int) -> Decimal:
    if _PRICE_PATTERN.fullmatch(value) is None:
        raise MofcomFreshSourceError(
            f"invalid MOFCOM current price in row {row_number}: {value!r}",
            error_code="PRICE_VALUE_INVALID",
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise MofcomFreshSourceError(
            f"invalid MOFCOM current price in row {row_number}: {value!r}",
            error_code="PRICE_VALUE_INVALID",
        ) from error
    if not parsed.is_finite() or parsed <= 0:
        raise MofcomFreshSourceError(
            f"MOFCOM current price must be positive in row {row_number}",
            error_code="PRICE_VALUE_INVALID",
        )
    return parsed


def _market_id_from_chart(
    cell: HtmlNode,
    *,
    commodity_id: str,
    source_date: date,
    page_url: str,
    row_number: int,
) -> str:
    links = [
        anchor.attrs.get("href", "").strip()
        for anchor in cell.find_all(lambda node: node.tag == "a")
        if "seachline.fhtml" in anchor.attrs.get("href", "")
    ]
    if len(links) != 1:
        raise MofcomFreshSourceError(
            f"expected one MOFCOM chart link in row {row_number}",
            error_code="MARKET_IDENTITY_INVALID",
        )
    chart_url = urlsplit(urljoin(page_url, links[0]))
    query = parse_qs(chart_url.query)
    market_ids = query.get("enterid", [])
    if (
        chart_url.hostname != "cif.mofcom.gov.cn"
        or chart_url.path != "/cif/seachline.fhtml"
        or len(market_ids) != 1
        or re.fullmatch(r"[1-9]\d*", market_ids[0]) is None
        or query.get("commdityid") != [commodity_id]
        or query.get("Edate") != [source_date.isoformat()]
    ):
        raise MofcomFreshSourceError(
            f"invalid MOFCOM chart identity in row {row_number}",
            error_code="MARKET_IDENTITY_INVALID",
        )
    return market_ids[0]


def _date_from_match(match: re.Match[str]) -> date:
    try:
        return date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError as error:
        raise MofcomFreshSourceError(
            "invalid date in MOFCOM source",
            error_code="SOURCE_DATE_INVALID",
        ) from error


def _source_day_to_utc(value: date) -> datetime:
    local_midnight = datetime.combine(value, time.min, tzinfo=_CHINA_TIME_ZONE)
    return local_midnight.astimezone(UTC).replace(tzinfo=None)


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", "", value).strip()


def _price_row_evidence_hash(*parts: str) -> str:
    """Hash source-row facts while the crawl record retains the complete HTML hash."""

    encoded = json.dumps(
        parts,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()
