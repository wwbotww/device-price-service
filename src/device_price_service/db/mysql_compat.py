from __future__ import annotations

import re
from typing import Any

from sqlalchemy import Engine, event

MINIMUM_MYSQL_VERSION = (5, 7, 8)
CHECK_CONSTRAINT_ENFORCEMENT_VERSION = (8, 0, 16)


class UnsupportedMySQLVersionError(RuntimeError):
    """Raised before application work when the MySQL server is too old."""


def parse_mysql_version(value: str) -> tuple[int, int, int]:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", value)
    if match is None:
        raise UnsupportedMySQLVersionError(f"cannot parse MySQL server version: {value}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def validate_mysql_version(value: str) -> tuple[int, int, int]:
    version = parse_mysql_version(value)
    if version < MINIMUM_MYSQL_VERSION:
        minimum = ".".join(str(part) for part in MINIMUM_MYSQL_VERSION)
        raise UnsupportedMySQLVersionError(
            f"MySQL {value} is unsupported; minimum version is {minimum}"
        )
    return version


def compatibility_mode(version: tuple[int, int, int]) -> str:
    if version < CHECK_CONSTRAINT_ENFORCEMENT_VERSION:
        return "mysql57-trigger-constraints"
    return "native-check-constraints"


def install_mysql_connection_guards(engine: Engine) -> None:
    event.listen(engine, "connect", _initialize_mysql_connection)


def _initialize_mysql_connection(
    dbapi_connection: Any,
    _connection_record: Any,
) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SELECT VERSION()")
        row = cursor.fetchone()
        if row is None:
            raise UnsupportedMySQLVersionError("MySQL server returned no version")
        validate_mysql_version(str(row[0]))
        # DATETIME has no timezone metadata. Force database-generated timestamps
        # to the same UTC convention used by application-supplied timestamps.
        cursor.execute("SET SESSION time_zone = '+00:00'")
    finally:
        cursor.close()
