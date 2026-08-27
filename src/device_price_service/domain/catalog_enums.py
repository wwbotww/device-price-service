from __future__ import annotations

from device_price_service.domain.enums import (
    Availability,
    LifecycleStatus,
    OperationStatus,
    OriginalPriceType,
    RunStatus,
    RunType,
    StringEnum,
)

__all__ = [
    "AccessMode",
    "Availability",
    "BusinessMode",
    "CatalogEntityType",
    "CollectionFetchMethod",
    "CollectionTriggerType",
    "ConditionCode",
    "FeeStatus",
    "ItemStatus",
    "ItemType",
    "LifecycleStatus",
    "MatchMethod",
    "MatchStatus",
    "MeasureType",
    "OperationStatus",
    "OriginalPriceType",
    "PriceNature",
    "PriceType",
    "PricingBasis",
    "QualityStatus",
    "RecordOrigin",
    "RegionMode",
    "RegionScope",
    "RunStatus",
    "RunType",
    "SellerType",
    "SourceType",
    "VerificationStatus",
]


class QualityStatus(StringEnum):
    ACCEPTED = "ACCEPTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"


class ItemType(StringEnum):
    MODEL = "MODEL"
    COMMODITY = "COMMODITY"
    GENERIC_GOOD = "GENERIC_GOOD"


class RecordOrigin(StringEnum):
    MANUAL = "MANUAL"
    RULE = "RULE"
    IMPORT = "IMPORT"
    AUTO = "AUTO"


class ItemStatus(StringEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    SUPERSEDED = "SUPERSEDED"


class ConditionCode(StringEnum):
    NEW = "NEW"
    USED = "USED"
    REFURBISHED = "REFURBISHED"
    UNKNOWN = "UNKNOWN"


class MeasureType(StringEnum):
    WEIGHT = "WEIGHT"
    VOLUME = "VOLUME"
    COUNT = "COUNT"
    LENGTH = "LENGTH"
    AREA = "AREA"
    SET = "SET"
    OTHER = "OTHER"


class SourceType(StringEnum):
    OFFICIAL_MALL = "OFFICIAL_MALL"
    MAJOR_ECOMMERCE = "MAJOR_ECOMMERCE"
    SUPERMARKET = "SUPERMARKET"
    PERSONAL_SITE = "PERSONAL_SITE"
    PUBLIC_DATA = "PUBLIC_DATA"


class BusinessMode(StringEnum):
    SELF_OPERATED = "SELF_OPERATED"
    MARKETPLACE = "MARKETPLACE"
    HYBRID = "HYBRID"
    WHOLESALE = "WHOLESALE"


class AccessMode(StringEnum):
    API = "API"
    HTTP = "HTTP"
    BROWSER = "BROWSER"
    FILE = "FILE"
    MIXED = "MIXED"


class RegionMode(StringEnum):
    NATIONAL = "NATIONAL"
    REGIONAL = "REGIONAL"
    MIXED = "MIXED"


class SellerType(StringEnum):
    PLATFORM_SELF = "PLATFORM_SELF"
    BRAND_OFFICIAL = "BRAND_OFFICIAL"
    THIRD_PARTY = "THIRD_PARTY"
    INDIVIDUAL = "INDIVIDUAL"
    PUBLIC_MARKET = "PUBLIC_MARKET"
    UNKNOWN = "UNKNOWN"


class VerificationStatus(StringEnum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    UNKNOWN = "UNKNOWN"


class RegionScope(StringEnum):
    NATIONAL = "NATIONAL"
    PROVINCE = "PROVINCE"
    CITY = "CITY"
    DISTRICT = "DISTRICT"
    DELIVERY_ZONE = "DELIVERY_ZONE"
    UNKNOWN = "UNKNOWN"
    MULTI = "MULTI"


class MatchStatus(StringEnum):
    CANDIDATE = "CANDIDATE"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    SUPERSEDED = "SUPERSEDED"


class MatchMethod(StringEnum):
    GTIN = "GTIN"
    MODEL = "MODEL"
    RULE = "RULE"
    MANUAL = "MANUAL"
    ML = "ML"


class PriceNature(StringEnum):
    RETAIL_OFFER = "RETAIL_OFFER"
    WHOLESALE_OFFER = "WHOLESALE_OFFER"
    RETAIL_AVERAGE = "RETAIL_AVERAGE"
    WHOLESALE_AVERAGE = "WHOLESALE_AVERAGE"
    MARKET_AVERAGE = "MARKET_AVERAGE"
    UNKNOWN = "UNKNOWN"


class PriceType(StringEnum):
    DIRECT_UNCONDITIONAL = "DIRECT_UNCONDITIONAL"
    PUBLISHED_VALUE = "PUBLISHED_VALUE"
    MEMBER = "MEMBER"
    COUPON = "COUPON"
    SUBSIDY = "SUBSIDY"
    STARTING = "STARTING"
    INSTALLMENT = "INSTALLMENT"
    DEPOSIT = "DEPOSIT"
    BUNDLE = "BUNDLE"
    UNKNOWN = "UNKNOWN"


class PricingBasis(StringEnum):
    PACKAGE_TOTAL = "PACKAGE_TOTAL"
    UNIT_QUOTED = "UNIT_QUOTED"
    VARIABLE_ESTIMATE = "VARIABLE_ESTIMATE"
    UNKNOWN = "UNKNOWN"


class FeeStatus(StringEnum):
    ITEM_ONLY = "ITEM_ONLY"
    SEPARATE_FEES_EXCLUDED = "SEPARATE_FEES_EXCLUDED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    INSEPARABLE = "INSEPARABLE"
    UNKNOWN = "UNKNOWN"


class CollectionTriggerType(StringEnum):
    SCHEDULED = "SCHEDULED"
    MANUAL = "MANUAL"
    QUERY_RECRAWL = "QUERY_RECRAWL"


class CatalogEntityType(StringEnum):
    CATEGORY = "CATEGORY"
    SEARCH = "SEARCH"
    LISTING = "LISTING"
    SKU = "SKU"
    OFFER = "OFFER"
    PUBLIC_PRICE = "PUBLIC_PRICE"


class CollectionFetchMethod(StringEnum):
    API = "API"
    HTTP = "HTTP"
    BROWSER = "BROWSER"
    FILE = "FILE"
    REPLAY = "REPLAY"
