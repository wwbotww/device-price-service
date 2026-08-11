import pytest

from device_price_service.db.mysql_compat import (
    UnsupportedMySQLVersionError,
    compatibility_mode,
    parse_mysql_version,
    validate_mysql_version,
)


def test_mysql_57_and_8_versions_are_parsed_and_classified() -> None:
    assert parse_mysql_version("5.7.36-log") == (5, 7, 36)
    assert parse_mysql_version("8.4.0") == (8, 4, 0)
    assert compatibility_mode(validate_mysql_version("5.7.36-log")) == (
        "mysql57-trigger-constraints"
    )
    assert compatibility_mode(validate_mysql_version("8.4.0")) == ("native-check-constraints")


@pytest.mark.parametrize("value", ["5.7.7", "5.6.51", "not-mysql"])
def test_unsupported_mysql_versions_fail_closed(value: str) -> None:
    with pytest.raises(UnsupportedMySQLVersionError):
        validate_mysql_version(value)
