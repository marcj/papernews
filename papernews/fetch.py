from __future__ import annotations

import calendar
import html
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

import feedparser
import requests


def _clean_title(s: str | None) -> str:
    """Decode HTML entities and collapse whitespace in feed-provided titles."""
    if not s:
        return ""
    return " ".join(html.unescape(s).split())


@dataclass
class RawItem:
    source: str
    url: str
    title: str
    # Date the source surfaced this item (HN submission for HN, feed pub for
    # RSS). Used for window filtering. The article's *own* publication date
    # comes from extract.py via trafilatura metadata.
    surfaced: str | None = None  # ISO date "YYYY-MM-DD" or None


# Algolia HN search. Returns stories matching the numericFilters, ranked by
# popularity. We then re-sort by points and truncate to `limit`.
_HN_SEARCH = "https://hn.algolia.com/api/v1/search"


def fetch_hn(
    source_name: str = "Hacker News",
    limit: int = 10,
    since_hours: int = 48,
    min_points: int = 50,
) -> Iterator[RawItem]:
    since = int(time.time() - since_hours * 3600)
    params = {
        "tags": "story",
        "numericFilters": [f"created_at_i>{since}", f"points>{min_points}"],
        "hitsPerPage": 100,
    }
    r = requests.get(_HN_SEARCH, params=params, timeout=15)
    r.raise_for_status()
    hits = r.json().get("hits", [])
    hits.sort(key=lambda h: h.get("points", 0), reverse=True)

    for h in hits[:limit]:
        title = _clean_title(h.get("title"))
        if not title:
            continue
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        ts = h.get("created_at_i")
        surfaced = (
            datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
            if ts else None
        )
        yield RawItem(source=source_name, url=url, title=title, surfaced=surfaced)


def fetch_wikipedia_events(
    source_name: str = "World news",
    days_back: int = 1,
) -> Iterator[RawItem]:
    """Yield one item per day of Wikipedia's Portal:Current_events.

    days_back=1 → just today. Increase to backfill recent days.
    """
    from datetime import date as _date, timedelta as _td
    from .wiki import current_events_url, current_events_title

    today = _date.today()
    for delta in range(days_back):
        day = today - _td(days=delta)
        yield RawItem(
            source=source_name,
            url=current_events_url(day),
            title=current_events_title(day),
            surfaced=day.isoformat(),
        )


def fetch_rss(
    source_name: str,
    feed_url: str,
    limit: int = 20,
    since_hours: int | None = None,
) -> Iterator[RawItem]:
    cutoff = time.time() - since_hours * 3600 if since_hours is not None else None
    d = feedparser.parse(feed_url)
    for entry in d.entries[:limit]:
        url = getattr(entry, "link", None)
        title = _clean_title(getattr(entry, "title", None))
        if not url or not title:
            continue
        parsed = (
            getattr(entry, "published_parsed", None)
            or getattr(entry, "updated_parsed", None)
        )
        if cutoff is not None and parsed is not None:
            if calendar.timegm(parsed) < cutoff:
                continue
        surfaced = time.strftime("%Y-%m-%d", parsed) if parsed else None
        yield RawItem(source=source_name, url=url, title=title, surfaced=surfaced)
