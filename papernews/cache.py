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


def edition_key(content_token: str, sources_config: list[dict]) -> str:
    """Stable hash representing 'which edition this is'."""
    payload = json.dumps(
        {
            "content": content_token,
            "sources": [
                {
                    "name": s.get("name"),
                    "kind": s.get("kind"),
                    "limit": s.get("limit"),
                }
                for s in sources_config
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def pdf_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"{key}.pdf"


def epub_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"{key}.epub"


def preview_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"{key}.png"


def ensure_dir(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir
