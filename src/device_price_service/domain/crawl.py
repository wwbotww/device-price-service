from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from device_price_service.domain.enums import FetchMethod


@dataclass(frozen=True, slots=True)
class FetchResult:
    request_url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    body: bytes
    fetched_at: datetime
    duration_ms: int
    fetch_method: FetchMethod

    @property
    def source_hash(self) -> str:
        return sha256(self.body).hexdigest()

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", maxsplit=1)[0].strip().lower()


@dataclass(frozen=True, slots=True)
class BrowserVariantDimension:
    name: str
    container_selector: str
    heading_text: str | tuple[str, ...]
    option_selector: str = "li"
    excluded_values: tuple[str, ...] = ()
    optional: bool = False


@dataclass(frozen=True, slots=True)
class BrowserFixedOption:
    container_selector: str
    value: str
    option_selector: str = "li"


@dataclass(frozen=True, slots=True)
class BrowserSnapshotPlan:
    ready_selector: str
    snapshot_selector: str
    dimensions: tuple[BrowserVariantDimension, ...]
    fixed_options: tuple[BrowserFixedOption, ...] = ()
    settle_ms: int = 300
    max_snapshots: int = 64


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    relative_path: str
    source_hash: str
    size_bytes: int
