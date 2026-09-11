from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from hashlib import sha256
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import xlrd  # type: ignore[import-untyped]

from device_price_service.crawlers.base import AdapterContext
from device_price_service.crawlers.catalog import CatalogDatasetConnector
from device_price_service.crawlers.html import parse_html
from device_price_service.db.catalog_seed import (
    SHANGHAI_FRESH_CHANNEL_CODE,
    SHANGHAI_FRESH_CONNECTOR_CODE,
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


class ShanghaiFreshSourceError(ValueError):
    """Raised when an official document cannot be mapped without guessing."""

    def __init__(self, message: str, *, error_code: str = "SHANGHAI_SOURCE_INVALID") -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class ShanghaiCommodityMapping:
    commodity_code: str
    commodity_group: str
    source_name: str
    source_specification: str
    source_unit: str = "元/500克"


SHANGHAI_FRESH_CATEGORY_CODE = "FRESH_MONITORED_COMMODITY"
SHANGHAI_FRESH_INDEX_URL = "https://fgw.sh.gov.cn/fgw_jgjgdt/"
SHANGHAI_FRESH_ALLOWED_DOMAINS = ("fgw.sh.gov.cn",)
SHANGHAI_FRESH_REGION_CODE = "310100"
SHANGHAI_FRESH_SHEET_NAME = "农贸市场、超市"

SHANGHAI_FIRST_COMMODITIES = (
    ShanghaiCommodityMapping("QINGCAI", "蔬菜", "青菜", "新鲜一级"),
    ShanghaiCommodityMapping("JIMAOCAI", "蔬菜", "鸡毛菜", "新鲜一级"),
    ShanghaiCommodityMapping("NAPA_CABBAGE", "蔬菜", "大白菜", "新鲜一级"),
    ShanghaiCommodityMapping("CUCUMBER", "蔬菜", "黄瓜", "新鲜一级"),
    ShanghaiCommodityMapping("CARROT", "蔬菜", "胡萝卜", "新鲜一级"),
    ShanghaiCommodityMapping("RED_FUJI_APPLE", "水果", "苹果", "红富士一级"),
    ShanghaiCommodityMapping("CHICKEN_EGG", "肉禽蛋", "鸡蛋", "新鲜完整"),
    ShanghaiCommodityMapping("LEAN_PORK", "肉禽蛋", "鲜猪肉", "精瘦肉"),
)

_ARTICLE_TITLE_PATTERN = re.compile(
    r"上海市主要主副食品品种价格信息表\s*[（(]"
    r"(?P<year>\d{4})年(?P<month>\d{2})月(?P<day>\d{2})日[）)]"
)
_WORKBOOK_DATE_PATTERN = re.compile(
    r"(?P<year>\d{4})年(?P<month>\d{2})月(?P<day>\d{2})日"
)
_PRICE_FORMAT_PATTERN = re.compile(r"0\.([0#]+)")
_PRICE_SCALE = Decimal("0.01")
_XLS_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
_SHANGHAI_TIME_ZONE = ZoneInfo("Asia/Shanghai")


class ShanghaiFreshRetailConnector(CatalogDatasetConnector):
    """Shanghai FDRC daily average-retail-price XLS connector."""

    channel_code = SHANGHAI_FRESH_CHANNEL_CODE
    connector_code = SHANGHAI_FRESH_CONNECTOR_CODE
    version = "shanghai-fresh-retail-1"
    fetch_method = CollectionFetchMethod.HTTP
    allowed_domains = SHANGHAI_FRESH_ALLOWED_DOMAINS

    async def discover_dataset(
        self,
        context: AdapterContext,
        request: CatalogCollectionRequest,
    ) -> DiscoveredCatalogDataset:
        self._validate_request(request)
        index_result = await context.http.fetch(
            SHANGHAI_FRESH_INDEX_URL,
            allowed_domains=context.allowed_domains,
        )
        self._require_html_success(index_result, label="index")
        source_date, article_url, article_title = self.parse_index(index_result.body)

        article_result = await context.http.fetch(
            article_url,
            allowed_domains=context.allowed_domains,
        )
        self._require_html_success(article_result, label="article")
        attachment_url = self.parse_article(
            article_result.body,
            article_url=article_result.final_url,
            expected_date=source_date,
        )
        source_observed_at = _source_day_to_utc(source_date)
        if source_observed_at > article_result.fetched_at + timedelta(minutes=5):
            raise ShanghaiFreshSourceError(
                "Shanghai source date is later than fetch time",
                error_code="SOURCE_DATE_MISMATCH",
            )
        return DiscoveredCatalogDataset(
            dataset_key=f"shanghai-fresh-retail:{source_date.isoformat()}",
            url=attachment_url,
            source_page_url=article_result.final_url,
            source_observed_at=source_observed_at,
            metadata={
                "source_date": source_date.isoformat(),
                "time_precision": "DAY",
                "article_title": article_title,
                "index_hash": index_result.source_hash,
                "article_hash": article_result.source_hash,
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
        )

    def parse_dataset(
        self,
        dataset: DiscoveredCatalogDataset,
        result: FetchResult,
    ) -> ParsedCatalogDataset:
        if not 200 <= result.status_code < 400:
            raise ShanghaiFreshSourceError(
                f"unexpected Shanghai XLS HTTP status: {result.status_code}"
            )
        if not result.body.startswith(_XLS_MAGIC):
            raise ShanghaiFreshSourceError(
                "Shanghai attachment is not an OLE2 XLS document",
                error_code="ATTACHMENT_INVALID",
            )
        if result.content_type not in {
            "application/vnd.ms-excel",
            "application/octet-stream",
            "",
        }:
            raise ShanghaiFreshSourceError(
                f"unexpected Shanghai XLS content type: {result.content_type!r}",
                error_code="ATTACHMENT_INVALID",
            )

        rows, price_formats = self._read_xls(result.body)
        return self.parse_table(
            dataset,
            rows=rows,
            price_formats=price_formats,
        )

    @staticmethod
    def parse_index(body: bytes) -> tuple[date, str, str]:
        root = parse_html(body)
        matches: dict[date, dict[str, str]] = {}
        for anchor in root.find_all(lambda node: node.tag == "a"):
            title = _compact_text(anchor.attrs.get("title") or anchor.text())
            match = _ARTICLE_TITLE_PATTERN.fullmatch(title)
            href = anchor.attrs.get("href", "").strip()
            if match is None or not href:
                continue
            source_date = _date_from_match(match)
            article_url = urljoin(SHANGHAI_FRESH_INDEX_URL, href)
            if urlsplit(article_url).hostname != "fgw.sh.gov.cn":
                raise ShanghaiFreshSourceError("Shanghai article link left the approved domain")
            matches.setdefault(source_date, {})[article_url] = title

        if not matches:
            raise ShanghaiFreshSourceError(
                "no Shanghai fresh-price article found on index",
                error_code="DISCOVERY_STRUCTURE_CHANGED",
            )
        latest_date = max(matches)
        latest = matches[latest_date]
        if len(latest) != 1:
            raise ShanghaiFreshSourceError(
                f"multiple Shanghai fresh-price articles found for {latest_date.isoformat()}",
                error_code="DISCOVERY_AMBIGUOUS",
            )
        article_url, title = next(iter(latest.items()))
        return latest_date, article_url, title

    @staticmethod
    def parse_article(
        body: bytes,
        *,
        article_url: str,
        expected_date: date,
    ) -> str:
        root = parse_html(body)
        title_candidates = [
            _compact_text(node.text())
            for node in root.find_all(
                lambda node: node.tag in {"h1", "h2"} and node.has_class("Article-title")
            )
        ]
        title_dates = {
            _date_from_match(match)
            for title in title_candidates
            if (match := _ARTICLE_TITLE_PATTERN.search(title)) is not None
        }
        if title_dates != {expected_date}:
            raise ShanghaiFreshSourceError(
                "Shanghai article date does not match index date",
                error_code="SOURCE_DATE_MISMATCH",
            )

        attachment_urls: set[str] = set()
        for anchor in root.find_all(lambda node: node.tag == "a"):
            href = anchor.attrs.get("href", "").strip()
            if not href or not urlsplit(href).path.lower().endswith(".xls"):
                continue
            attachment_url = urljoin(article_url, href)
            if urlsplit(attachment_url).hostname != "fgw.sh.gov.cn":
                raise ShanghaiFreshSourceError("Shanghai attachment left the approved domain")
            attachment_urls.add(attachment_url)
        if len(attachment_urls) != 1:
            raise ShanghaiFreshSourceError(
                f"expected one Shanghai XLS attachment, found {len(attachment_urls)}",
                error_code="DISCOVERY_STRUCTURE_CHANGED",
            )
        return next(iter(attachment_urls))

    @staticmethod
    def _read_xls(body: bytes) -> tuple[list[list[object]], dict[int, str]]:
        try:
            workbook = xlrd.open_workbook(file_contents=body, formatting_info=True)
            sheet = workbook.sheet_by_name(SHANGHAI_FRESH_SHEET_NAME)
        except (xlrd.XLRDError, IndexError, KeyError) as error:
            raise ShanghaiFreshSourceError(
                "cannot read expected Shanghai XLS sheet",
                error_code="ATTACHMENT_INVALID",
            ) from error

        rows = [sheet.row_values(row_index) for row_index in range(sheet.nrows)]
        price_row_index = _find_labeled_row(rows, "均价")
        formats: dict[int, str] = {}
        for column_index in range(sheet.ncols):
            xf_index = sheet.cell_xf_index(price_row_index, column_index)
            format_key = workbook.xf_list[xf_index].format_key
            formats[column_index] = workbook.format_map[format_key].format_str
        return rows, formats

    @staticmethod
    def parse_table(
        dataset: DiscoveredCatalogDataset,
        *,
        rows: list[list[object]],
        price_formats: dict[int, str],
    ) -> ParsedCatalogDataset:
        source_date = _dataset_source_date(dataset)
        workbook_dates = _workbook_dates(rows)
        if workbook_dates != {source_date}:
            raise ShanghaiFreshSourceError(
                "Shanghai workbook date does not match its article date",
                error_code="SOURCE_DATE_MISMATCH",
            )

        category_row = rows[_find_labeled_row(rows, "类别")]
        name_row = rows[_find_labeled_row(rows, "品种")]
        specification_row = rows[_find_labeled_row(rows, "规格")]
        unit_row = rows[_find_labeled_row(rows, "单位")]
        price_row = rows[_find_labeled_row(rows, "均价")]
        width = max(
            len(category_row),
            len(name_row),
            len(specification_row),
            len(unit_row),
            len(price_row),
        )

        parsed_rows: list[ParsedCatalogRow] = []
        for mapping in SHANGHAI_FIRST_COMMODITIES:
            matching_columns = [
                column
                for column in range(2, width)
                if _cell_text(category_row, column) == mapping.commodity_group
                and _cell_text(name_row, column) == mapping.source_name
                and _cell_text(specification_row, column) == mapping.source_specification
                and _cell_text(unit_row, column) == mapping.source_unit
            ]
            if len(matching_columns) != 1:
                raise ShanghaiFreshSourceError(
                    "expected exactly one Shanghai column for "
                    f"{mapping.source_name}/{mapping.source_specification}/{mapping.source_unit}, "
                    f"found {len(matching_columns)}",
                    error_code="COMMODITY_MAPPING_MISSING",
                )
            column = matching_columns[0]
            published_price = _published_price(
                _cell(price_row, column),
                price_formats.get(column, ""),
            )
            parsed_rows.append(
                _parsed_commodity_row(
                    dataset,
                    mapping=mapping,
                    published_price=published_price,
                )
            )
        return ParsedCatalogDataset(rows=parsed_rows)

    @staticmethod
    def _validate_request(request: CatalogCollectionRequest) -> None:
        if (
            request.region_scope is not RegionScope.CITY
            or request.region_code != SHANGHAI_FRESH_REGION_CODE
        ):
            raise ShanghaiFreshSourceError(
                "Shanghai fresh retail connector only supports CITY/310100",
                error_code="SCOPE_UNSUPPORTED",
            )
        if request.category_codes and SHANGHAI_FRESH_CATEGORY_CODE not in request.category_codes:
            raise ShanghaiFreshSourceError(
                f"Shanghai fresh retail connector requires {SHANGHAI_FRESH_CATEGORY_CODE}",
                error_code="SCOPE_UNSUPPORTED",
            )
        if request.source_item_codes:
            raise ShanghaiFreshSourceError(
                "Shanghai fresh retail dataset does not support source item selection",
                error_code="SCOPE_UNSUPPORTED",
            )

    @staticmethod
    def _require_html_success(result: FetchResult, *, label: str) -> None:
        if not 200 <= result.status_code < 400:
            raise ShanghaiFreshSourceError(
                f"unexpected Shanghai {label} HTTP status: {result.status_code}"
            )
        if result.content_type not in {"text/html", "application/xhtml+xml", ""}:
            raise ShanghaiFreshSourceError(
                f"unexpected Shanghai {label} content type: {result.content_type!r}"
            )


def _parsed_commodity_row(
    dataset: DiscoveredCatalogDataset,
    *,
    mapping: ShanghaiCommodityMapping,
    published_price: Decimal,
) -> ParsedCatalogRow:
    listing_key = ":".join(
        (
            "shanghai-city-retail-average",
            mapping.commodity_code,
            mapping.source_specification,
            mapping.source_unit,
        )
    )
    item = DiscoveredCatalogListing(
        listing_key=listing_key,
        url=dataset.source_page_url,
        category_code=SHANGHAI_FRESH_CATEGORY_CODE,
        merchant=SourceMerchant(
            merchant_key="shanghai-fresh-price-monitoring",
            name="上海市主要主副食品价格监测",
            seller_type=SellerType.PUBLIC_MARKET,
            verification_status=VerificationStatus.VERIFIED,
        ),
        price_nature=PriceNature.RETAIL_AVERAGE,
        external_product_id=mapping.commodity_code,
        external_sku_id=mapping.commodity_code,
    )
    parsed = ParsedCatalogListing(
        source_title=(
            f"{mapping.source_name} {mapping.source_specification} {mapping.source_unit}"
        ),
        source_category_path=f"生鲜食品/{mapping.commodity_group}/{mapping.source_name}",
        source_attributes={
            "commodity_code": mapping.commodity_code,
            "commodity_name": mapping.source_name,
            "commodity_group": mapping.commodity_group,
            "source_specification": mapping.source_specification,
            "quoted_unit": mapping.source_unit,
            "net_content": "500克",
            "time_precision": "DAY",
        },
        price_candidates=[
            SourcePriceCandidate(
                current_price=published_price,
                price_type=PriceType.PUBLISHED_VALUE,
                pricing_basis=PricingBasis.UNIT_QUOTED,
                availability=Availability.UNKNOWN,
                fee_status=FeeStatus.NOT_APPLICABLE,
                displayed_price_text=f"{published_price:.2f}元/500克",
                evidence_hash=_price_row_evidence_hash(
                    "SHANGHAI_FRESH_RETAIL",
                    _dataset_source_date(dataset).isoformat(),
                    mapping.commodity_code,
                    mapping.commodity_group,
                    mapping.source_name,
                    mapping.source_specification,
                    mapping.source_unit,
                    SHANGHAI_FRESH_REGION_CODE,
                    f"{published_price:.2f}",
                ),
                region=CatalogRegion(
                    scope=RegionScope.CITY,
                    code=SHANGHAI_FRESH_REGION_CODE,
                ),
                source_observed_at=dataset.source_observed_at,
            )
        ],
    )
    return ParsedCatalogRow(item=item, parsed=parsed)


def _dataset_source_date(dataset: DiscoveredCatalogDataset) -> date:
    raw = dataset.metadata.get("source_date")
    if not isinstance(raw, str):
        raise ShanghaiFreshSourceError(
            "Shanghai dataset is missing source_date metadata",
            error_code="SOURCE_DATE_INVALID",
        )
    try:
        source_date = date.fromisoformat(raw)
    except ValueError as error:
        raise ShanghaiFreshSourceError(
            "Shanghai source_date metadata is invalid",
            error_code="SOURCE_DATE_INVALID",
        ) from error
    if dataset.source_observed_at != _source_day_to_utc(source_date):
        raise ShanghaiFreshSourceError(
            "Shanghai source date and observation time disagree",
            error_code="SOURCE_DATE_MISMATCH",
        )
    return source_date


def _workbook_dates(rows: list[list[object]]) -> set[date]:
    dates: set[date] = set()
    for row in rows[:8]:
        for value in row[:2]:
            for match in _WORKBOOK_DATE_PATTERN.finditer(str(value)):
                dates.add(_date_from_match(match))
    if not dates:
        raise ShanghaiFreshSourceError(
            "Shanghai workbook does not contain a source date",
            error_code="SOURCE_DATE_INVALID",
        )
    return dates


def _find_labeled_row(rows: list[list[object]], label: str) -> int:
    matches = [index for index, row in enumerate(rows) if _cell_text(row, 0) == label]
    if len(matches) != 1:
        raise ShanghaiFreshSourceError(
            f"expected one Shanghai row labelled {label!r}, found {len(matches)}",
            error_code="DATASET_STRUCTURE_CHANGED",
        )
    return matches[0]


def _published_price(value: object, number_format: str) -> Decimal:
    match = _PRICE_FORMAT_PATTERN.search(number_format)
    if match is None or len(match.group(1)) != 2:
        raise ShanghaiFreshSourceError(
            f"Shanghai average price format is not two decimal places: {number_format!r}",
            error_code="PRICE_VALUE_INVALID",
        )
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as error:
        raise ShanghaiFreshSourceError(
            f"invalid Shanghai average price: {value!r}",
            error_code="PRICE_VALUE_INVALID",
        ) from error
    if not parsed.is_finite() or parsed <= 0:
        raise ShanghaiFreshSourceError(
            "Shanghai average price must be a positive number",
            error_code="PRICE_VALUE_INVALID",
        )
    return parsed.quantize(_PRICE_SCALE, rounding=ROUND_HALF_UP)


def _cell(row: list[object], column: int) -> object:
    return row[column] if column < len(row) else ""


def _cell_text(row: list[object], column: int) -> str:
    return _compact_text(str(_cell(row, column)))


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", "", value).strip()


def _price_row_evidence_hash(*parts: str) -> str:
    """Hash source-row facts while the crawl record retains the complete XLS hash."""

    encoded = json.dumps(
        parts,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _date_from_match(match: re.Match[str]) -> date:
    try:
        return date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError as error:
        raise ShanghaiFreshSourceError("invalid date in Shanghai source") from error


def _source_day_to_utc(value: date) -> datetime:
    local_midnight = datetime.combine(value, time.min, tzinfo=_SHANGHAI_TIME_ZONE)
    return local_midnight.astimezone(UTC).replace(tzinfo=None)
