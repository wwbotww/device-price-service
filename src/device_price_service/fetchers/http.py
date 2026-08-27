from __future__ import annotations

import asyncio
import random
import ssl
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx

from device_price_service.config import Settings
from device_price_service.domain.crawl import FetchResult
from device_price_service.domain.enums import FetchMethod
from device_price_service.domain.models import utc_now_naive
from device_price_service.fetchers.url_policy import UrlPolicy


class HttpFetchError(RuntimeError):
    """Raised when no HTTP response can be obtained after bounded retries."""


class ResponseTooLargeError(HttpFetchError):
    """Raised when a response exceeds the configured evidence limit."""


class TooManyRedirectsError(HttpFetchError):
    """Raised when a redirect chain exceeds the configured maximum."""


TlsProfile = Literal["DEFAULT", "TLS12_COMPAT"]


class DomainThrottle:
    def __init__(self, *, concurrency: int, min_delay: float, max_delay: float) -> None:
        self.concurrency = concurrency
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._timing_locks: dict[str, asyncio.Lock] = {}
        self._next_allowed: dict[str, float] = {}

    @asynccontextmanager
    async def slot(self, url: str) -> AsyncIterator[None]:
        host = urlsplit(url).hostname or ""
        semaphore = self._semaphores.setdefault(host, asyncio.Semaphore(self.concurrency))
        timing_lock = self._timing_locks.setdefault(host, asyncio.Lock())
        async with semaphore:
            async with timing_lock:
                now = time.monotonic()
                wait_seconds = max(0.0, self._next_allowed.get(host, now) - now)
                if wait_seconds:
                    await asyncio.sleep(wait_seconds)
                delay = random.uniform(self.min_delay, self.max_delay)
                self._next_allowed[host] = time.monotonic() + delay
            yield


class HttpFetcher:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        retry_attempts: int = 3,
    ) -> None:
        self.settings = settings
        self.retry_attempts = retry_attempts
        self.throttle = DomainThrottle(
            concurrency=settings.http_concurrency_per_domain,
            min_delay=settings.http_min_delay_seconds,
            max_delay=settings.http_max_delay_seconds,
        )
        self._owns_client = client is None
        self.client = client or self._new_client()
        self._tls12_compat_client: httpx.AsyncClient | None = None

    def _new_client(
        self,
        *,
        verify: ssl.SSLContext | bool = True,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=self.settings.http_connect_timeout_seconds,
                read=self.settings.http_read_timeout_seconds,
                write=self.settings.http_read_timeout_seconds,
                pool=self.settings.http_connect_timeout_seconds,
            ),
            headers={
                "User-Agent": self.settings.crawler_user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            follow_redirects=False,
            trust_env=False,
            verify=verify,
        )

    async def __aenter__(self) -> HttpFetcher:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
        if self._tls12_compat_client is not None:
            await self._tls12_compat_client.aclose()

    async def fetch(
        self,
        url: str,
        *,
        allowed_domains: list[str],
        tls_profile: TlsProfile = "DEFAULT",
    ) -> FetchResult:
        policy = UrlPolicy(allowed_domains)
        policy.validate(url)
        started = time.monotonic()
        last_error: httpx.RequestError | None = None
        client = self._client_for_profile(tls_profile)

        for attempt in range(1, self.retry_attempts + 1):
            try:
                result = await self._fetch_redirect_chain(url, policy, client=client)
            except httpx.RequestError as error:
                last_error = error
                if attempt == self.retry_attempts:
                    break
                await asyncio.sleep(self._backoff_seconds(attempt))
                continue

            if result.status_code not in {429, 500, 502, 503, 504}:
                return replace(result, duration_ms=int((time.monotonic() - started) * 1000))
            if attempt == self.retry_attempts:
                return replace(result, duration_ms=int((time.monotonic() - started) * 1000))
            await asyncio.sleep(self._retry_delay(result, attempt))

        raise HttpFetchError(
            f"request failed after {self.retry_attempts} attempts: {url}"
        ) from last_error

    async def _fetch_redirect_chain(
        self,
        url: str,
        policy: UrlPolicy,
        *,
        client: httpx.AsyncClient,
    ) -> FetchResult:
        current_url = url
        for redirect_count in range(self.settings.http_max_redirects + 1):
            policy.validate(current_url)
            async with self.throttle.slot(current_url):
                response = await client.get(current_url)
            body = response.content
            if len(body) > self.settings.http_max_response_bytes:
                raise ResponseTooLargeError(
                    f"response exceeds {self.settings.http_max_response_bytes} bytes"
                )

            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    return self._to_result(url, current_url, response, body)
                if redirect_count == self.settings.http_max_redirects:
                    raise TooManyRedirectsError(f"too many redirects for {url}")
                next_url = urljoin(current_url, location)
                policy.validate(next_url)
                current_url = next_url
                continue
            return self._to_result(url, current_url, response, body)

        raise TooManyRedirectsError(f"too many redirects for {url}")

    def _client_for_profile(self, profile: TlsProfile) -> httpx.AsyncClient:
        if profile == "DEFAULT":
            return self.client
        if profile != "TLS12_COMPAT":
            raise ValueError(f"unsupported TLS profile: {profile}")
        if self._tls12_compat_client is None:
            self._tls12_compat_client = self._new_client(
                verify=_tls12_compat_context()
            )
        return self._tls12_compat_client

    @staticmethod
    def _to_result(
        request_url: str,
        final_url: str,
        response: httpx.Response,
        body: bytes,
    ) -> FetchResult:
        return FetchResult(
            request_url=request_url,
            final_url=final_url,
            status_code=response.status_code,
            headers={key.lower(): value for key, value in response.headers.items()},
            body=body,
            fetched_at=utc_now_naive(),
            duration_ms=0,
            fetch_method=FetchMethod.HTTP,
        )

    @staticmethod
    def _backoff_seconds(attempt: int) -> float:
        return float(min(8.0, 2 ** (attempt - 1)) + random.uniform(0, 0.5))

    def _retry_delay(self, result: FetchResult, attempt: int) -> float:
        retry_after = result.headers.get("retry-after")
        if retry_after:
            seconds = self._parse_retry_after(retry_after, result.fetched_at)
            if seconds is not None:
                return min(30.0, max(0.0, seconds))
        return self._backoff_seconds(attempt)

    @staticmethod
    def _parse_retry_after(value: str, now: datetime) -> float | None:
        if value.isdigit():
            return float(value)
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        return (parsed - now).total_seconds()


def _tls12_compat_context() -> ssl.SSLContext:
    """Keep certificate verification while supporting a narrowly scoped legacy endpoint."""

    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers("AES128-GCM-SHA256")
    return context
