from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from device_price_service.domain.enums import OriginalPriceType


class PricePolicyError(ValueError):
    """Base error for ambiguous or disallowed price text."""


class ConditionalPriceError(PricePolicyError):
    """Raised when a candidate requires a subsidy or other condition."""


class AmbiguousPriceError(PricePolicyError):
    """Raised when a candidate contains more than one possible amount."""


class MoneyParseError(PricePolicyError):
    """Raised when no explicit monetary amount can be found."""


@dataclass(frozen=True, slots=True)
class PriceCandidate:
    text: str
    label: str = ""


@dataclass(frozen=True, slots=True)
class PriceResolution:
    original_price: Decimal | None
    original_price_type: OriginalPriceType
    current_price: Decimal | None


class PricePolicy:
    _CONDITIONAL_TERMS = (
        "国补",
        "补贴",
        "券后",
        "领券",
        "优惠券",
        "会员",
        "以旧换新",
        "换购",
        "月供",
        "分期",
        "低至",
        "起售价",
        "元起",
        " 起",
        "到手价",
        "最高优惠",
        "预估",
        "定金",
    )
    _MONEY_PATTERN = re.compile(
        r"(?:RMB|CNY|[¥￥])\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)"
        r"|([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*元",
        re.IGNORECASE,
    )
    _PLAIN_NUMBER = re.compile(r"^[0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?$")
    _STARTING_AMOUNT = re.compile(r"[0-9](?:\s*元)?\s*起")

    def resolve(
        self,
        *,
        original: PriceCandidate | None,
        current: PriceCandidate | None,
    ) -> PriceResolution:
        original_price = (
            self.parse_candidate(original, allow_conditional=False) if original else None
        )
        current_price = self.parse_candidate(current, allow_conditional=False) if current else None
        return PriceResolution(
            original_price=original_price,
            original_price_type=self._original_type(original)
            if original
            else OriginalPriceType.NONE,
            current_price=current_price,
        )

    def parse_candidate(
        self,
        candidate: PriceCandidate,
        *,
        allow_conditional: bool = False,
    ) -> Decimal:
        combined = f"{candidate.label} {candidate.text}".strip()
        if not allow_conditional and (
            any(term in combined for term in self._CONDITIONAL_TERMS)
            or self._STARTING_AMOUNT.search(combined)
        ):
            raise ConditionalPriceError(f"conditional price is excluded: {combined}")

        raw_amounts = [first or second for first, second in self._MONEY_PATTERN.findall(combined)]
        if not raw_amounts and self._PLAIN_NUMBER.fullmatch(candidate.text.strip()):
            raw_amounts = [candidate.text.strip()]
        amounts = {self._to_decimal(value) for value in raw_amounts}
        if not amounts:
            raise MoneyParseError(f"no explicit monetary amount found: {combined}")
        if len(amounts) > 1:
            raise AmbiguousPriceError(f"multiple monetary amounts found: {combined}")
        amount = amounts.pop()
        if amount <= 0:
            raise MoneyParseError("price must be greater than zero")
        return amount

    @staticmethod
    def _to_decimal(value: str) -> Decimal:
        try:
            return Decimal(value.replace(",", "")).quantize(Decimal("0.01"))
        except InvalidOperation as error:
            raise MoneyParseError(f"invalid monetary amount: {value}") from error

    @staticmethod
    def _original_type(candidate: PriceCandidate) -> OriginalPriceType:
        combined = f"{candidate.label} {candidate.text}"
        if "划线" in combined:
            return OriginalPriceType.CROSSED_OUT
        if "建议" in combined or "零售价" in combined:
            return OriginalPriceType.MSRP
        return OriginalPriceType.EXPLICIT_ORIGINAL
