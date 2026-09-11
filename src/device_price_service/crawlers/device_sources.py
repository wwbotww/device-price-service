"""Approved official device sources shared by seeding and collection entry points."""

from dataclasses import dataclass

from device_price_service.domain.catalog_enums import AccessMode


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
    allowed_domains: tuple[str, ...]
    connector_code: str
    access_mode: AccessMode


BRANDS = (
    BrandSeed("APPLE", "Apple", "Apple"),
    BrandSeed("HUAWEI", "华为", "Huawei"),
    BrandSeed("XIAOMI", "小米", "Xiaomi"),
    BrandSeed("OPPO", "OPPO", "OPPO"),
    BrandSeed("VIVO", "vivo", "vivo"),
)

CHANNELS = (
    ChannelSeed(
        "APPLE",
        "APPLE_CN_WEB",
        "Apple 中国大陆在线商店",
        "https://www.apple.com.cn/shop/",
        ("www.apple.com.cn",),
        "apple-cn",
        AccessMode.HTTP,
    ),
    ChannelSeed(
        "HUAWEI",
        "HUAWEI_CN_WEB",
        "华为商城",
        "https://www.vmall.com/",
        ("www.vmall.com", "m.vmall.com", "item.vmall.com", "openapi.vmall.com"),
        "huawei-cn",
        AccessMode.MIXED,
    ),
    ChannelSeed(
        "XIAOMI",
        "XIAOMI_CN_WEB",
        "小米商城",
        "https://www.mi.com/shop/",
        ("www.mi.com",),
        "xiaomi-cn",
        AccessMode.MIXED,
    ),
    ChannelSeed(
        "OPPO",
        "OPPO_CN_WEB",
        "OPPO 商城",
        "https://www.opposhop.cn/",
        ("www.opposhop.cn",),
        "oppo-cn",
        AccessMode.API,
    ),
    ChannelSeed(
        "VIVO",
        "VIVO_CN_WEB",
        "vivo 官方商城",
        "https://shop.vivo.com.cn/",
        ("shop.vivo.com.cn",),
        "vivo-cn",
        AccessMode.API,
    ),
)
