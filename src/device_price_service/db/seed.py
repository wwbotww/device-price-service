from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from device_price_service.db.models import Brand, Category, SalesChannel


@dataclass(frozen=True)
class BrandSeed:
    code: str
    name_zh: str
    name_en: str


@dataclass(frozen=True)
class ChannelSeed:
    brand_code: str
    code: str
    name: str
    base_url: str
    allowed_domains: list[str]


BRANDS = (
    BrandSeed("APPLE", "Apple", "Apple"),
    BrandSeed("HUAWEI", "华为", "Huawei"),
    BrandSeed("XIAOMI", "小米", "Xiaomi"),
    BrandSeed("OPPO", "OPPO", "OPPO"),
    BrandSeed("VIVO", "vivo", "vivo"),
)

CATEGORIES = (
    ("PHONE", "手机", None),
    ("TABLET", "平板", None),
    ("COMPUTER", "电脑", None),
    ("LAPTOP", "笔记本电脑", "COMPUTER"),
    ("DESKTOP", "台式电脑", "COMPUTER"),
    ("WATCH", "手表", None),
)

CHANNELS = (
    ChannelSeed(
        "APPLE",
        "APPLE_CN_WEB",
        "Apple 中国大陆在线商店",
        "https://www.apple.com.cn/shop/",
        ["www.apple.com.cn"],
    ),
    ChannelSeed(
        "HUAWEI",
        "HUAWEI_CN_WEB",
        "华为商城",
        "https://www.vmall.com/",
        ["www.vmall.com", "m.vmall.com", "item.vmall.com", "openapi.vmall.com"],
    ),
    ChannelSeed(
        "XIAOMI",
        "XIAOMI_CN_WEB",
        "小米商城",
        "https://www.mi.com/shop/",
        ["www.mi.com"],
    ),
    ChannelSeed(
        "OPPO",
        "OPPO_CN_WEB",
        "OPPO 商城",
        "https://www.opposhop.cn/",
        ["www.opposhop.cn"],
    ),
    ChannelSeed(
        "VIVO",
        "VIVO_CN_WEB",
        "vivo 官方商城",
        "https://shop.vivo.com.cn/",
        ["shop.vivo.com.cn"],
    ),
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
                allowed_domains=channel_seed.allowed_domains,
            )
            session.add(channel)
        else:
            channel.brand_id = brands_by_code[channel_seed.brand_code].id
            channel.name = channel_seed.name
            channel.base_url = channel_seed.base_url
            channel.allowed_domains = channel_seed.allowed_domains
            channel.region_code = "CN"
            channel.currency = "CNY"
            channel.seller_type = "OFFICIAL_DIRECT"
            channel.enabled = True

    session.flush()
