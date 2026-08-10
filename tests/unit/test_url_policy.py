import pytest

from device_price_service.fetchers.url_policy import UrlPolicy, UrlPolicyError


def test_url_policy_accepts_exact_https_domain() -> None:
    policy = UrlPolicy(["shop.example.cn"])
    assert policy.validate("https://shop.example.cn/products/1")


@pytest.mark.parametrize(
    "url",
    [
        "http://shop.example.cn/products/1",
        "https://sub.shop.example.cn/products/1",
        "https://shop.example.cn:8443/products/1",
        "https://user:password@shop.example.cn/products/1",
        "https://evil.example/products/1",
    ],
)
def test_url_policy_rejects_non_allowlisted_urls(url: str) -> None:
    with pytest.raises(UrlPolicyError):
        UrlPolicy(["shop.example.cn"]).validate(url)
