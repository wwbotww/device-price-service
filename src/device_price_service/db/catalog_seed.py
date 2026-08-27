from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from device_price_service.db.catalog_models import SourceChannel, TaxonomyCategory
from device_price_service.db.catalog_repositories import GeneralCatalogRepository
from device_price_service.domain.catalog_enums import (
    AccessMode,
    BusinessMode,
    MeasureType,
    RegionMode,
    SourceType,
)

SHANGHAI_FRESH_CHANNEL_CODE = "SH_FGW_FRESH_RETAIL"
SHANGHAI_FRESH_CONNECTOR_CODE = "shanghai-fresh-retail"
MOFCOM_FRESH_CHANNEL_CODE = "MOFCOM_FRESH_WHOLESALE"
MOFCOM_FRESH_CONNECTOR_CODE = "mofcom-fresh-wholesale"


@dataclass(frozen=True, slots=True)
class CatalogCategorySeed:
    code: str
    name_zh: str
    level: int
    path: str
    is_leaf: bool
    attribute_profile_code: str
    attribute_profile_version: str
    parent_code: str | None = None
    default_measure_type: MeasureType | None = None


FRESH_CATEGORY_SEEDS = (
    CatalogCategorySeed("FOOD", "食品", 0, "/FOOD/", False, "category-root", "1"),
    CatalogCategorySeed(
        "FRESH_FOOD",
        "生鲜食品",
        1,
        "/FOOD/FRESH/",
        False,
        "category-root",
        "1",
        parent_code="FOOD",
    ),
    CatalogCategorySeed(
        "FRESH_FRUIT",
        "新鲜水果",
        2,
        "/FOOD/FRESH/FRUIT/",
        False,
        "category-root",
        "1",
        parent_code="FRESH_FOOD",
        default_measure_type=MeasureType.WEIGHT,
    ),
    CatalogCategorySeed(
        "FRESH_APPLE",
        "鲜苹果",
        3,
        "/FOOD/FRESH/FRUIT/APPLE/",
        True,
        "fresh-apple",
        "1",
        parent_code="FRESH_FRUIT",
        default_measure_type=MeasureType.WEIGHT,
    ),
    CatalogCategorySeed(
        "FRESH_EGG",
        "鲜蛋",
        2,
        "/FOOD/FRESH/EGG/",
        True,
        "packaged-egg",
        "1",
        parent_code="FRESH_FOOD",
        default_measure_type=MeasureType.COUNT,
    ),
    CatalogCategorySeed(
        "FRESH_MEAT",
        "鲜肉",
        2,
        "/FOOD/FRESH/MEAT/",
        False,
        "category-root",
        "1",
        parent_code="FRESH_FOOD",
        default_measure_type=MeasureType.WEIGHT,
    ),
    CatalogCategorySeed(
        "FRESH_PORK",
        "鲜猪肉",
        3,
        "/FOOD/FRESH/MEAT/PORK/",
        True,
        "fresh-pork",
        "1",
        parent_code="FRESH_MEAT",
        default_measure_type=MeasureType.WEIGHT,
    ),
    CatalogCategorySeed(
        "FRESH_MONITORED_COMMODITY",
        "政府监测生鲜品种",
        2,
        "/FOOD/FRESH/MONITORED_COMMODITY/",
        True,
        "government-fresh",
        "1",
        parent_code="FRESH_FOOD",
        default_measure_type=MeasureType.WEIGHT,
    ),
)


def seed_fresh_categories(session: Session) -> dict[str, TaxonomyCategory]:
    """Idempotently seed only the category profiles implemented in phase D."""

    catalog = GeneralCatalogRepository(session)
    categories: dict[str, TaxonomyCategory] = {}
    for seed in FRESH_CATEGORY_SEEDS:
        parent = categories.get(seed.parent_code) if seed.parent_code else None
        category = catalog.get_or_create_category(
            code=seed.code,
            name_zh=seed.name_zh,
            level=seed.level,
            path=seed.path,
            is_leaf=seed.is_leaf,
            attribute_profile_code=seed.attribute_profile_code,
            attribute_profile_version=seed.attribute_profile_version,
            parent_id=parent.id if parent is not None else None,
            default_measure_type=seed.default_measure_type,
        )
        categories[seed.code] = category
    return categories


def seed_shanghai_fresh_source(
    session: Session,
    *,
    enable: bool = False,
) -> SourceChannel:
    """Seed the first government channel disabled unless explicitly enabled."""

    channel = GeneralCatalogRepository(session).get_or_create_source_channel(
        code=SHANGHAI_FRESH_CHANNEL_CODE,
        name="上海市主要主副食品平均零售价",
        source_type=SourceType.PUBLIC_DATA,
        business_mode=BusinessMode.SELF_OPERATED,
        access_mode=AccessMode.HTTP,
        base_url="https://fgw.sh.gov.cn/fgw_jgjgdt/",
        allowed_domains=["fgw.sh.gov.cn"],
        region_mode=RegionMode.REGIONAL,
        connector_code=SHANGHAI_FRESH_CONNECTOR_CODE,
        enabled=enable,
    )
    if enable:
        channel.enabled = True
    return channel


def seed_mofcom_fresh_source(
    session: Session,
    *,
    enable: bool = False,
) -> SourceChannel:
    """Seed the MOFCOM market wholesale channel disabled unless explicitly enabled."""

    channel = GeneralCatalogRepository(session).get_or_create_source_channel(
        code=MOFCOM_FRESH_CHANNEL_CODE,
        name="商务部百家日报农副产品批发价格",
        source_type=SourceType.PUBLIC_DATA,
        business_mode=BusinessMode.WHOLESALE,
        access_mode=AccessMode.HTTP,
        base_url="https://cif.mofcom.gov.cn/cif/seach.fhtml",
        allowed_domains=["cif.mofcom.gov.cn"],
        region_mode=RegionMode.MIXED,
        connector_code=MOFCOM_FRESH_CONNECTOR_CODE,
        enabled=enable,
    )
    if enable:
        channel.enabled = True
    return channel
