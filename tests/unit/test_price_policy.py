from decimal import Decimal

import pytest

from device_price_service.domain.enums import OriginalPriceType
from device_price_service.domain.price_policy import (
    AmbiguousPriceError,
    ConditionalPriceError,
    PriceCandidate,
    PricePolicy,
)


def test_price_policy_resolves_explicit_original_and_current_price() -> None:
    resolution = PricePolicy().resolve(
        original=PriceCandidate("￥9,999", "建议零售价"),
        current=PriceCandidate("8,999 元", "售价"),
    )
    assert resolution.original_price == Decimal("9999.00")
    assert resolution.original_price_type is OriginalPriceType.MSRP
    assert resolution.current_price == Decimal("8999.00")


@pytest.mark.parametrize(
    "text",
    [
        "国补价 ¥7,999",
        "券后 7999 元",
        "会员价 ¥7,999",
        "以旧换新低至 6999 元",
        "24 期月供 333 元",
        "7999 元起",
        "¥7999 起",
        "到手价 ¥7999",
    ],
)
def test_price_policy_rejects_conditional_prices(text: str) -> None:
    with pytest.raises(ConditionalPriceError):
        PricePolicy().parse_candidate(PriceCandidate(text, "售价"))


def test_price_policy_rejects_ambiguous_amounts() -> None:
    with pytest.raises(AmbiguousPriceError):
        PricePolicy().parse_candidate(PriceCandidate("原价 9999 元，现价 8999 元", "售价"))


@pytest.mark.parametrize("text", ["RMB 9999起", "¥9,999.00起", "CNY 9999\u00a0起", "9999 元 起"])
def test_price_policy_rejects_starting_amount_with_or_without_spacing(text: str) -> None:
    with pytest.raises(ConditionalPriceError):
        PricePolicy().parse_candidate(PriceCandidate(text, "售价"))


def test_price_policy_does_not_treat_unrelated_start_character_as_price_condition() -> None:
    assert PricePolicy().parse_candidate(PriceCandidate("RMB 9999", "一起购买设备售价")) == Decimal(
        "9999.00"
    )
