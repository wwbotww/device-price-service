from __future__ import annotations

import json
import re
import unicodedata
from hashlib import sha256
from typing import Any


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", normalized).strip()


def normalize_capacity(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = normalize_text(value).upper().replace(" ", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(GB|TB)", normalized)
    return f"{match.group(1)}{match.group(2)}" if match else normalize_text(value)


def build_spec_fingerprint(attributes: dict[str, Any]) -> str:
    canonical = _canonicalize(attributes)
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            normalize_text(str(key)).lower(): _canonicalize(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    if isinstance(value, str):
        return normalize_text(value)
    return value
