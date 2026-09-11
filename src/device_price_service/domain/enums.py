from enum import StrEnum


class StringEnum(StrEnum):
    pass


class LifecycleStatus(StringEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    UNKNOWN = "UNKNOWN"


class Availability(StringEnum):
    ON_SALE = "ON_SALE"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    RESERVATION = "RESERVATION"
    PRE_SALE = "PRE_SALE"
    COMING_SOON = "COMING_SOON"
    OFF_SHELF = "OFF_SHELF"
    UNKNOWN = "UNKNOWN"


class OriginalPriceType(StringEnum):
    CROSSED_OUT = "CROSSED_OUT"
    MSRP = "MSRP"
    EXPLICIT_ORIGINAL = "EXPLICIT_ORIGINAL"
    NONE = "NONE"


class RunType(StringEnum):
    DISCOVERY = "DISCOVERY"
    PRICE = "PRICE"
    FULL = "FULL"
    REPLAY = "REPLAY"


class RunStatus(StringEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class FetchMethod(StringEnum):
    HTTP = "HTTP"
    BROWSER = "BROWSER"
    REPLAY = "REPLAY"


class OperationStatus(StringEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
