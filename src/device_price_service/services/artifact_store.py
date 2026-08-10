from __future__ import annotations

import gzip
import os
import re
import tempfile
from hashlib import sha256
from pathlib import Path

from device_price_service.domain.crawl import ArtifactReference, FetchResult


class ArtifactError(RuntimeError):
    """Base error for raw evidence persistence and verification."""


class ArtifactIntegrityError(ArtifactError):
    """Raised when stored evidence no longer matches its recorded hash."""


class RawArtifactStore:
    _SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, *, brand_code: str, crawl_run_id: int, result: FetchResult) -> ArtifactReference:
        segment = brand_code.lower()
        if not self._SAFE_SEGMENT.fullmatch(segment):
            raise ArtifactError("brand_code contains unsafe path characters")
        day = result.fetched_at
        target_dir = (
            self.root / segment / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}" / str(crawl_run_id)
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        extension = self._extension(result.content_type)
        target = target_dir / f"{result.source_hash}.{extension}.gz"

        if not target.exists():
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(dir=target_dir, delete=False) as temporary:
                    temporary_path = Path(temporary.name)
                    with gzip.GzipFile(fileobj=temporary, mode="wb", mtime=0) as archive:
                        archive.write(result.body)
                os.replace(temporary_path, target)
            finally:
                if temporary_path is not None and temporary_path.exists():
                    temporary_path.unlink()

        return ArtifactReference(
            relative_path=str(target.relative_to(self.root)),
            source_hash=result.source_hash,
            size_bytes=len(result.body),
        )

    def load(self, relative_path: str, *, expected_hash: str | None = None) -> bytes:
        candidate = (self.root / relative_path).resolve()
        if not candidate.is_relative_to(self.root):
            raise ArtifactError("artifact path escapes the configured storage root")
        if not candidate.is_file():
            raise ArtifactError(f"artifact does not exist: {relative_path}")
        with gzip.open(candidate, "rb") as archive:
            body = archive.read()
        actual_hash = sha256(body).hexdigest()
        if expected_hash is not None and actual_hash != expected_hash.lower():
            raise ArtifactIntegrityError(
                f"artifact hash mismatch: expected {expected_hash}, got {actual_hash}"
            )
        return body

    @staticmethod
    def _extension(content_type: str) -> str:
        if content_type in {"application/json", "application/ld+json"}:
            return "json"
        return "html"
