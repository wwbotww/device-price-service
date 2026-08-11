from __future__ import annotations

import json
import re
import time
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page, Route, async_playwright

from device_price_service.config import Settings
from device_price_service.domain.crawl import (
    BrowserFixedOption,
    BrowserSnapshotPlan,
    BrowserVariantDimension,
    FetchResult,
)
from device_price_service.domain.enums import FetchMethod
from device_price_service.domain.models import utc_now_naive
from device_price_service.fetchers.http import ResponseTooLargeError
from device_price_service.fetchers.url_policy import UrlPolicy, UrlPolicyError


class BrowserFetchError(RuntimeError):
    """Raised when a browser-rendered page cannot be fetched safely."""


class BrowserFetcher:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def fetch(self, url: str, *, allowed_domains: list[str]) -> FetchResult:
        policy = UrlPolicy(allowed_domains)
        policy.validate(url)
        started = time.monotonic()

        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(
                        user_agent=self.settings.crawler_user_agent,
                        locale="zh-CN",
                    )
                    page = await context.new_page()

                    async def enforce_navigation_policy(route: Route) -> None:
                        request = route.request
                        if request.is_navigation_request() and request.frame == page.main_frame:
                            try:
                                policy.validate(request.url)
                            except UrlPolicyError:
                                await route.abort("blockedbyclient")
                                return
                        await route.continue_()

                    await page.route("**/*", enforce_navigation_policy)
                    response = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=self.settings.browser_timeout_ms,
                    )
                    policy.validate(page.url)
                    await page.wait_for_timeout(self.settings.browser_render_settle_ms)
                    body = (await page.content()).encode("utf-8")
                    if len(body) > self.settings.http_max_response_bytes:
                        raise ResponseTooLargeError(
                            f"rendered page exceeds {self.settings.http_max_response_bytes} bytes"
                        )
                    headers = await response.all_headers() if response is not None else {}
                    status = response.status if response is not None else 0
                    return FetchResult(
                        request_url=url,
                        final_url=page.url,
                        status_code=status,
                        headers={key.lower(): value for key, value in headers.items()},
                        body=body,
                        fetched_at=utc_now_naive(),
                        duration_ms=int((time.monotonic() - started) * 1000),
                        fetch_method=FetchMethod.BROWSER,
                    )
                finally:
                    await browser.close()
        except (PlaywrightError, UrlPolicyError) as error:
            raise BrowserFetchError(f"browser fetch failed for {url}") from error

    async def fetch_snapshots(
        self,
        url: str,
        *,
        allowed_domains: list[str],
        plan: BrowserSnapshotPlan,
    ) -> FetchResult:
        """Render every requested variant combination into one replayable evidence envelope."""
        policy = UrlPolicy(allowed_domains)
        policy.validate(url)
        started = time.monotonic()
        if plan.max_snapshots < 1 or plan.max_snapshots > 256:
            raise ValueError("browser snapshot max must be between 1 and 256")

        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(
                        user_agent=self.settings.crawler_user_agent,
                        locale="zh-CN",
                    )
                    page = await context.new_page()
                    await self._install_navigation_policy(page, policy)
                    response = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=self.settings.browser_timeout_ms,
                    )
                    policy.validate(page.url)
                    await page.locator(plan.ready_selector).first.wait_for(
                        state="visible",
                        timeout=self.settings.browser_timeout_ms,
                    )
                    for fixed in plan.fixed_options:
                        await self._select_fixed_option(page, fixed, plan.settle_ms)

                    snapshots: list[dict[str, Any]] = []
                    seen: set[tuple[tuple[str, str], ...]] = set()
                    await self._walk_dimensions(
                        page=page,
                        plan=plan,
                        dimension_index=0,
                        snapshots=snapshots,
                        seen=seen,
                    )
                    if not snapshots:
                        raise BrowserFetchError(f"no variant snapshots captured for {url}")

                    envelope = {
                        "schema": "device-price-browser-snapshots-v1",
                        "source_url": page.url,
                        "page_title": await page.title(),
                        "snapshots": snapshots,
                    }
                    body = json.dumps(
                        envelope,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    if len(body) > self.settings.http_max_response_bytes:
                        raise ResponseTooLargeError(
                            "snapshot evidence exceeds "
                            f"{self.settings.http_max_response_bytes} bytes"
                        )
                    headers = await response.all_headers() if response is not None else {}
                    headers["content-type"] = "application/json"
                    headers["x-device-price-artifact"] = "browser-snapshots-v1"
                    return FetchResult(
                        request_url=url,
                        final_url=page.url,
                        status_code=response.status if response is not None else 0,
                        headers={key.lower(): value for key, value in headers.items()},
                        body=body,
                        fetched_at=utc_now_naive(),
                        duration_ms=int((time.monotonic() - started) * 1000),
                        fetch_method=FetchMethod.BROWSER,
                    )
                finally:
                    await browser.close()
        except (PlaywrightError, UrlPolicyError) as error:
            raise BrowserFetchError(f"browser snapshot fetch failed for {url}") from error

    async def _walk_dimensions(
        self,
        *,
        page: Page,
        plan: BrowserSnapshotPlan,
        dimension_index: int,
        snapshots: list[dict[str, Any]],
        seen: set[tuple[tuple[str, str], ...]],
    ) -> None:
        if len(snapshots) >= plan.max_snapshots:
            raise BrowserFetchError(
                f"variant combinations exceed configured maximum {plan.max_snapshots}"
            )
        if dimension_index == len(plan.dimensions):
            selections = await self._active_selections(page, plan.dimensions)
            key = tuple(sorted(selections.items()))
            if key in seen:
                return
            root_html = await page.locator(plan.snapshot_selector).first.evaluate(
                "element => element.outerHTML"
            )
            seen.add(key)
            snapshots.append({"selections": selections, "html": root_html})
            return

        dimension = plan.dimensions[dimension_index]
        container = self._dimension_container(page, dimension)
        if await container.count() == 0:
            if not dimension.optional:
                # A preceding selection can remove an incompatible lower-level
                # dimension from the DOM. That branch has no valid variant, but
                # other branches must still be enumerated.
                if dimension_index > 0:
                    return
                raise BrowserFetchError(f"variant dimension is missing: {dimension.name}")
            await self._walk_dimensions(
                page=page,
                plan=plan,
                dimension_index=dimension_index + 1,
                snapshots=snapshots,
                seen=seen,
            )
            return
        options = container.locator(dimension.option_selector)
        values = await self._option_values(options)
        if not values:
            if not dimension.optional:
                if dimension_index > 0:
                    return
                raise BrowserFetchError(f"variant dimension is empty: {dimension.name}")
            await self._walk_dimensions(
                page=page,
                plan=plan,
                dimension_index=dimension_index + 1,
                snapshots=snapshots,
                seen=seen,
            )
            return
        excluded = set(dimension.excluded_values)
        for value in values:
            if value in excluded:
                continue
            # Variant clicks commonly re-render the whole option list. Re-resolve the
            # container for every iteration and skip combinations removed by the page.
            container = self._dimension_container(page, dimension)
            options = container.locator(dimension.option_selector)
            option = options.filter(has_text=re.compile(rf"^\s*{re.escape(value)}\s*$")).first
            if await option.count() == 0:
                continue
            classes = (await option.get_attribute("class") or "").lower()
            aria_disabled = (await option.get_attribute("aria-disabled") or "").lower()
            if "disabled" in classes or aria_disabled == "true":
                continue
            await option.click(timeout=self.settings.browser_timeout_ms)
            await page.wait_for_timeout(plan.settle_ms)
            await self._walk_dimensions(
                page=page,
                plan=plan,
                dimension_index=dimension_index + 1,
                snapshots=snapshots,
                seen=seen,
            )

    @staticmethod
    async def _option_values(options: Locator) -> list[str]:
        raw_values: list[str] = await options.evaluate_all(
            "elements => elements.map(element => "
            "(element.getAttribute('title') || element.textContent || '').trim())"
        )
        return list(dict.fromkeys(value for value in raw_values if value))

    @staticmethod
    def _dimension_container(page: Page, dimension: BrowserVariantDimension) -> Locator:
        headings = (
            (dimension.heading_text,)
            if isinstance(dimension.heading_text, str)
            else dimension.heading_text
        )
        heading_pattern = re.compile("|".join(re.escape(heading) for heading in headings))
        return page.locator(dimension.container_selector).filter(has_text=heading_pattern).first

    async def _active_selections(
        self,
        page: Page,
        dimensions: tuple[BrowserVariantDimension, ...],
    ) -> dict[str, str]:
        selections: dict[str, str] = {}
        for dimension in dimensions:
            container = self._dimension_container(page, dimension)
            if await container.count() == 0 and dimension.optional:
                continue
            active = container.locator(f"{dimension.option_selector}.active").first
            if await active.count() == 0 and dimension.optional:
                continue
            value = (await active.get_attribute("title") or await active.inner_text()).strip()
            if not value:
                raise BrowserFetchError(f"active variant is empty: {dimension.name}")
            selections[dimension.name] = value
        return selections

    async def _select_fixed_option(
        self,
        page: Page,
        fixed: BrowserFixedOption,
        settle_ms: int,
    ) -> None:
        options = page.locator(fixed.container_selector).first.locator(fixed.option_selector)
        option = options.filter(has_text=re.compile(rf"^\s*{re.escape(fixed.value)}\s*$")).first
        await option.click(timeout=self.settings.browser_timeout_ms)
        await page.wait_for_timeout(settle_ms)

    @staticmethod
    async def _install_navigation_policy(page: Page, policy: UrlPolicy) -> None:
        async def enforce_navigation_policy(route: Route) -> None:
            request = route.request
            if request.is_navigation_request() and request.frame == page.main_frame:
                try:
                    policy.validate(request.url)
                except UrlPolicyError:
                    await route.abort("blockedbyclient")
                    return
            await route.continue_()

        await page.route("**/*", enforce_navigation_policy)
