"""V2-only reference data for the five approved mainland official device sources."""

from sqlalchemy.orm import Session

from device_price_service.crawlers.device_sources import BRANDS, CHANNELS
from device_price_service.db.catalog_models import SourceChannel
from device_price_service.db.catalog_repositories import GeneralCatalogRepository
from device_price_service.db.catalog_seed import CatalogCategorySeed
from device_price_service.domain.catalog_enums import (
    BusinessMode,
    MeasureType,
    RegionMode,
    SourceType,
)
from device_price_service.normalization.devices import DeviceCategoryRule

DEVICE_CATEGORY_SEEDS = (
    CatalogCategorySeed("ELECTRONICS", "电子设备", 0, "/ELECTRONICS/", False, "category-root", "1"),
    CatalogCategorySeed(
        "COMPUTER",
        "电脑",
        1,
        "/ELECTRONICS/COMPUTER/",
        False,
        "category-root",
        "1",
        parent_code="ELECTRONICS",
    ),
    *(
        CatalogCategorySeed(
            code,
            name,
            2 if parent == "COMPUTER" else 1,
            f"/ELECTRONICS/{'COMPUTER/' if parent == 'COMPUTER' else ''}{code}/",
            True,
            DeviceCategoryRule.profile_code,
            DeviceCategoryRule.version,
            parent_code=parent,
            default_measure_type=MeasureType.COUNT,
        )
        for code, name, parent in (
            ("PHONE", "手机", "ELECTRONICS"),
            ("TABLET", "平板", "ELECTRONICS"),
            ("LAPTOP", "笔记本电脑", "COMPUTER"),
            ("DESKTOP", "台式电脑", "COMPUTER"),
            ("WATCH", "手表", "ELECTRONICS"),
        )
    ),
)


def seed_device_catalog(session: Session, *, enable: bool = False) -> list[SourceChannel]:
    """Do not read V1, reset existing source switches, or alter fresh-food references."""
    catalog = GeneralCatalogRepository(session)
    for seed in BRANDS:
        catalog.get_or_create_brand(code=seed.code, name_zh=seed.name_zh, name_en=seed.name_en)
    category_ids: dict[str, int] = {}
    for category_seed in DEVICE_CATEGORY_SEEDS:
        category = catalog.get_or_create_category(
            code=category_seed.code,
            name_zh=category_seed.name_zh,
            level=category_seed.level,
            path=category_seed.path,
            is_leaf=category_seed.is_leaf,
            parent_id=category_ids[category_seed.parent_code]
            if category_seed.parent_code
            else None,
            default_measure_type=category_seed.default_measure_type,
            attribute_profile_code=category_seed.attribute_profile_code,
            attribute_profile_version=category_seed.attribute_profile_version,
        )
        category_ids[category_seed.code] = category.id
    channels = []
    for source in CHANNELS:
        channel = catalog.get_or_create_source_channel(
            code=source.code,
            name=source.name,
            source_type=SourceType.OFFICIAL_MALL,
            business_mode=BusinessMode.SELF_OPERATED,
            access_mode=source.access_mode,
            base_url=source.base_url,
            allowed_domains=list(source.allowed_domains),
            region_mode=RegionMode.NATIONAL,
            connector_code=source.connector_code,
            enabled=enable,
        )
        if enable:
            channel.enabled = True
        channels.append(channel)
    session.flush()
    return channels
