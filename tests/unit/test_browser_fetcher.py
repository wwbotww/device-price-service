import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import device_price_service.fetchers.browser as browser_module
from device_price_service.config import Settings
from device_price_service.domain.crawl import BrowserSnapshotPlan
from device_price_service.domain.enums import FetchMethod
from device_price_service.fetchers.browser import BrowserFetcher, BrowserFetchError
from device_price_service.fetchers.http import ResponseTooLargeError
from device_price_service.fetchers.url_policy import UrlPolicyError


def test_browser_fetcher_rejects_url_before_launching_browser() -> None:
    fetcher = BrowserFetcher(Settings(_env_file=None))

    async def run() -> None:
        with pytest.raises(UrlPolicyError):
            await fetcher.fetch("https://evil.example/product", allowed_domains=["shop.example.cn"])

    asyncio.run(run())


def _mock_browser(
    monkeypatch: pytest.MonkeyPatch,
    status: int | None,
    *,
    final_url: str = "https://shop.example.cn/product/fixture",
) -> tuple[SimpleNamespace, SimpleNamespace]:
    response = (
        SimpleNamespace(
            status=status,
            all_headers=AsyncMock(return_value={"Content-Type": "text/html; charset=utf-8"}),
        )
        if status is not None
        else None
    )
    page = SimpleNamespace(
        url=final_url,
        route=AsyncMock(),
        goto=AsyncMock(return_value=response),
        content=AsyncMock(return_value="<html><h1>商品暂不可用</h1></html>"),
        title=AsyncMock(return_value="Fixture product"),
        locator=Mock(side_effect=AssertionError("error pages must not wait for product selectors")),
        wait_for_timeout=AsyncMock(),
    )
    browser = SimpleNamespace(
        new_context=AsyncMock(return_value=SimpleNamespace(new_page=AsyncMock(return_value=page))),
        close=AsyncMock(),
    )
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
    manager = SimpleNamespace()

    class MockPlaywrightContext:
        async def __aenter__(self) -> SimpleNamespace:
            return playwright

        async def __aexit__(self, *args: object) -> None:
            manager.closed = True

    monkeypatch.setattr(browser_module, "async_playwright", MockPlaywrightContext)
    return page, browser


@pytest.mark.parametrize("method", ["fetch", "fetch_snapshots"])
@pytest.mark.parametrize("status", [403, 404, 410, 500, None])
def test_browser_error_response_preserves_html_status_and_urls_without_variant_waits(
    monkeypatch: pytest.MonkeyPatch, method: str, status: int | None
) -> None:
    page, browser = _mock_browser(
        monkeypatch, status, final_url="https://shop.example.cn/unavailable"
    )
    fetcher = BrowserFetcher(Settings(_env_file=None))
    kwargs = {"allowed_domains": ["shop.example.cn"]}
    if method == "fetch_snapshots":
        kwargs["plan"] = BrowserSnapshotPlan(".ready", ".product", ())
    result = asyncio.run(
        getattr(fetcher, method)("https://shop.example.cn/product/fixture", **kwargs)
    )
    assert result.status_code == (status or 0)
    assert result.request_url == "https://shop.example.cn/product/fixture"
    assert result.final_url == page.url
    assert result.body == "<html><h1>商品暂不可用</h1></html>".encode()
    assert result.fetch_method is FetchMethod.BROWSER
    assert result.headers == (
        {"content-type": "text/html; charset=utf-8"} if status is not None else {}
    )
    assert "x-device-price-artifact" not in result.headers
    page.locator.assert_not_called()
    page.wait_for_timeout.assert_not_awaited()
    browser.close.assert_awaited_once()


@pytest.mark.parametrize("method", ["fetch", "fetch_snapshots"])
def test_browser_error_response_still_enforces_size_limit_and_closes_browser(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    page, browser = _mock_browser(monkeypatch, 404)
    page.content.return_value = "x" * 1025
    fetcher = BrowserFetcher(Settings(_env_file=None, http_max_response_bytes=1024))
    kwargs = {"allowed_domains": ["shop.example.cn"]}
    if method == "fetch_snapshots":
        kwargs["plan"] = BrowserSnapshotPlan(".ready", ".product", ())
    with pytest.raises(ResponseTooLargeError, match="rendered page exceeds"):
        asyncio.run(getattr(fetcher, method)(page.url, **kwargs))
    page.locator.assert_not_called()
    browser.close.assert_awaited_once()


@pytest.mark.parametrize("method", ["fetch", "fetch_snapshots"])
def test_browser_error_response_still_rejects_external_redirects(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    page, browser = _mock_browser(monkeypatch, 404, final_url="https://evil.example/product")
    fetcher = BrowserFetcher(Settings(_env_file=None))
    kwargs = {"allowed_domains": ["shop.example.cn"]}
    if method == "fetch_snapshots":
        kwargs["plan"] = BrowserSnapshotPlan(".ready", ".product", ())
    with pytest.raises(BrowserFetchError):
        asyncio.run(getattr(fetcher, method)("https://shop.example.cn/product/fixture", **kwargs))
    page.content.assert_not_awaited()
    page.locator.assert_not_called()
    browser.close.assert_awaited_once()


def test_browser_successful_page_still_waits_for_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    page, browser = _mock_browser(monkeypatch, 200)
    fetcher = BrowserFetcher(Settings(_env_file=None, browser_render_settle_ms=25))
    result = asyncio.run(fetcher.fetch(page.url, allowed_domains=["shop.example.cn"]))
    assert result.status_code == 200
    page.wait_for_timeout.assert_awaited_once_with(25)
    page.content.assert_awaited_once()
    browser.close.assert_awaited_once()


def test_browser_successful_snapshot_still_waits_and_enumerates_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, browser = _mock_browser(monkeypatch, 200)
    ready = SimpleNamespace(wait_for=AsyncMock())
    page.locator = Mock(return_value=SimpleNamespace(first=ready))
    fetcher = BrowserFetcher(Settings(_env_file=None))

    async def enumerate_variants(**kwargs: object) -> None:
        kwargs["snapshots"].append({"selections": {}, "html": "<main>fixture</main>"})

    walk = AsyncMock(side_effect=enumerate_variants)
    monkeypatch.setattr(fetcher, "_walk_dimensions", walk)
    result = asyncio.run(
        fetcher.fetch_snapshots(
            page.url,
            allowed_domains=["shop.example.cn"],
            plan=BrowserSnapshotPlan(".ready", ".product", ()),
        )
    )
    assert result.status_code == 200
    assert json.loads(result.body)["snapshots"] == [
        {"selections": {}, "html": "<main>fixture</main>"}
    ]
    assert result.content_type == "application/json"
    ready.wait_for.assert_awaited_once()
    walk.assert_awaited_once()
    page.content.assert_not_awaited()
    browser.close.assert_awaited_once()


def test_browser_snapshot_fetcher_rejects_url_before_launching_browser() -> None:
    fetcher = BrowserFetcher(Settings(_env_file=None))
    plan = BrowserSnapshotPlan(
        ready_selector=".ready",
        snapshot_selector=".product",
        dimensions=(),
    )

    async def run() -> None:
        with pytest.raises(UrlPolicyError):
            await fetcher.fetch_snapshots(
                "https://evil.example/product",
                allowed_domains=["shop.example.cn"],
                plan=plan,
            )

    asyncio.run(run())
