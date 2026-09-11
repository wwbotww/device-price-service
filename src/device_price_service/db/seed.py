from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from device_price_service.crawlers.device_sources import BRANDS, CHANNELS
from device_price_service.db.models import Brand, Category, SalesChannel

CATEGORIES = (
    ("PHONE", "手机", None),
    ("TABLET", "平板", None),
    ("COMPUTER", "电脑", None),
    ("LAPTOP", "笔记本电脑", "COMPUTER"),
    ("DESKTOP", "台式电脑", "COMPUTER"),
    ("WATCH", "手表", None),
)

def seed_reference_data(session: Session) -> None:
    brands_by_code: dict[str, Brand] = {}
    for brand_seed in BRANDS:
        brand = session.scalar(select(Brand).where(Brand.code == brand_seed.code))
        if brand is None:
            brand = Brand(
                code=brand_seed.code,
                name_zh=brand_seed.name_zh,
                name_en=brand_seed.name_en,
            )
            session.add(brand)
            session.flush()
        else:
            brand.name_zh = brand_seed.name_zh
            brand.name_en = brand_seed.name_en
            brand.enabled = True
        brands_by_code[brand_seed.code] = brand

    categories_by_code: dict[str, Category] = {}
    for code, name_zh, _ in CATEGORIES:
        category = session.scalar(select(Category).where(Category.code == code))
        if category is None:
            category = Category(code=code, name_zh=name_zh)
            session.add(category)
            session.flush()
        else:
            category.name_zh = name_zh
            category.enabled = True
        categories_by_code[code] = category

    for code, _, parent_code in CATEGORIES:
        categories_by_code[code].parent_id = (
            categories_by_code[parent_code].id if parent_code is not None else None
        )

    for channel_seed in CHANNELS:
        channel = session.scalar(select(SalesChannel).where(SalesChannel.code == channel_seed.code))
        if channel is None:
            channel = SalesChannel(
                brand_id=brands_by_code[channel_seed.brand_code].id,
                code=channel_seed.code,
                name=channel_seed.name,
                base_url=channel_seed.base_url,
                allowed_domains=list(channel_seed.allowed_domains),
            )
            session.add(channel)
        else:
            channel.brand_id = brands_by_code[channel_seed.brand_code].id
            channel.name = channel_seed.name
            channel.base_url = channel_seed.base_url
            channel.allowed_domains = list(channel_seed.allowed_domains)
            channel.region_code = "CN"
            channel.currency = "CNY"
            channel.seller_type = "OFFICIAL_DIRECT"
            channel.enabled = True

    session.flush()
