"""On-disk cache for the current edition's PDF + cover preview.

The current edition is determined by:
  - the high-water mark of new content (max fetched_at in the store)
  - the sources config (sources.toml hashed)

When either changes, the cache key changes and a rebuild is triggered.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _source_fields(s: dict) -> dict:
    """The parts of a source config that change which articles get rendered."""
    fields = {
        "name": s.get("name"),
        "kind": s.get("kind"),
        "limit": s.get("limit"),
    }
    # since_hours changes which stored articles make the edition, so it has
    # to move the key too — but only include it when actually set, so configs
    # that don't use it keep the keys their cached PDFs were built under.
    if s.get("since_hours") is not None:
        fields["since_hours"] = s["since_hours"]
    return fields


def edition_key(content_token: str, sources_config: list[dict]) -> str:
    """Stable hash representing 'which edition this is'."""
    payload = json.dumps(
        {
            "content": content_token,
            "sources": [_source_fields(s) for s in sources_config],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def pdf_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"{key}.pdf"


def preview_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"{key}.png"


def ensure_dir(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir
