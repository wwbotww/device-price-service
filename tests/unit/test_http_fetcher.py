import asyncio

import httpx
import pytest

from device_price_service.config import Settings
from device_price_service.fetchers.http import HttpFetcher, ResponseTooLargeError
from device_price_service.fetchers.url_policy import UrlPolicyError


def _settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        http_min_delay_seconds=0,
        http_max_delay_seconds=0,
        **overrides,
    )


def test_http_fetcher_follows_only_validated_redirects() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            fetcher = HttpFetcher(_settings(), client=client, retry_attempts=1)
            result = await fetcher.fetch(
                "https://shop.example.cn/start", allowed_domains=["shop.example.cn"]
            )
            assert result.final_url == "https://shop.example.cn/final"
            assert result.body == b"{}"
            assert result.content_type == "application/json"

    asyncio.run(run())
    assert requested == ["https://shop.example.cn/start", "https://shop.example.cn/final"]


def test_http_fetcher_blocks_redirect_before_external_request() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/collect"})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            fetcher = HttpFetcher(_settings(), client=client, retry_attempts=1)
            with pytest.raises(UrlPolicyError):
                await fetcher.fetch(
                    "https://shop.example.cn/start", allowed_domains=["shop.example.cn"]
                )

    asyncio.run(run())
    assert requested == ["https://shop.example.cn/start"]


def test_http_fetcher_rejects_oversized_response() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1025)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            fetcher = HttpFetcher(
                _settings(http_max_response_bytes=1024), client=client, retry_attempts=1
            )
            with pytest.raises(ResponseTooLargeError):
                await fetcher.fetch(
                    "https://shop.example.cn/product", allowed_domains=["shop.example.cn"]
                )

    asyncio.run(run())
