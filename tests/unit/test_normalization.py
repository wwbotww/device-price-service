from device_price_service.normalization.specs import (
    build_spec_fingerprint,
    normalize_capacity,
    normalize_text,
)


def test_spec_fingerprint_is_stable_across_key_order_and_whitespace() -> None:
    first = build_spec_fingerprint({"Color": " 星夜黑 ", "capacity": "256GB"})
    second = build_spec_fingerprint({"capacity": "256GB", "color": "星夜黑"})
    assert first == second


def test_text_and_capacity_normalization() -> None:
    assert normalize_text("  iPhone\n  Pro  ") == "iPhone Pro"
    assert normalize_capacity(" 1 tb ") == "1TB"
