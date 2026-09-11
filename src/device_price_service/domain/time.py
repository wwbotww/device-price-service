"""UTC timestamps shared by fetching and catalog observations."""

from datetime import UTC, datetime


def utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
