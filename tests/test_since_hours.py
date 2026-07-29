"""Tests for the per-source `since_hours` age filter (PR #7).

The filter is applied at two layers, and both are covered here:

    1. gather time  — fetch_rss() drops feed entries older than the window
    2. render time  — Store.latest_per_source(since_date=...) drops stored
                      articles older than the window (rows are never deleted,
                      so without this the edition would accumulate stale ones)

Also covers the two things that surround it: the edition cache key has to
move when since_hours changes, and fetch_hn's Algolia numericFilters have to
be encoded so that *both* filters survive.

No network, no Anthropic SDK, no xelatex — feedparser and requests are
stubbed.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from papernews.cache import edition_key
from papernews.store import Store


def _struct_hours_ago(hours: float) -> time.struct_time:
    """UTC struct_time N hours in the past, as feedparser would produce."""
    return time.gmtime(time.time() - hours * 3600)


class _Entry:
    """Minimal stand-in for a feedparser entry."""

    def __init__(self, link, title, published_parsed=None):
        self.link = link
        self.title = title
        if published_parsed is not None:
            self.published_parsed = published_parsed


class _Feed:
    def __init__(self, entries):
        self.entries = entries


# --- 1: gather-time filtering ---------------------------------------------


class FetchRssSinceHoursTests(unittest.TestCase):
    def _fetch(self, entries, **kwargs):
        from papernews.fetch import fetch_rss

        with mock.patch("papernews.fetch.feedparser.parse",
                        return_value=_Feed(entries)):
            return list(fetch_rss("Test", "http://example.invalid/feed", **kwargs))

    def test_no_since_hours_keeps_everything(self):
        entries = [
            _Entry("http://a.invalid/1", "Fresh", _struct_hours_ago(1)),
            _Entry("http://a.invalid/2", "Ancient", _struct_hours_ago(5000)),
        ]
        self.assertEqual(len(self._fetch(entries)), 2)

    def test_drops_entries_older_than_window(self):
        entries = [
            _Entry("http://a.invalid/1", "Fresh", _struct_hours_ago(1)),
            _Entry("http://a.invalid/2", "Stale", _struct_hours_ago(100)),
        ]
        got = self._fetch(entries, since_hours=24)
        self.assertEqual([i.title for i in got], ["Fresh"])

    def test_entries_without_a_date_are_kept(self):
        """Documented behaviour: no date == can't judge == keep."""
        entries = [
            _Entry("http://a.invalid/1", "Undated"),
            _Entry("http://a.invalid/2", "Stale", _struct_hours_ago(100)),
        ]
        got = self._fetch(entries, since_hours=24)
        self.assertEqual([i.title for i in got], ["Undated"])

    @unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
    def test_cutoff_is_utc_not_local_time(self):
        """feedparser's *_parsed structs are UTC, so the comparison must use
        calendar.timegm, not time.mktime (which reads a struct as *local*
        time and skews the cutoff by the host's offset).

        The host timezone is forced here rather than inherited: under TZ=UTC
        — which is what CI usually runs — timegm and mktime agree and the
        assertion would pass either way. At UTC+14 a 2h-old entry looks 16h
        old to mktime, so a 6h window wrongly drops it.
        """
        prev_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Etc/GMT-14"  # UTC+14
        time.tzset()
        try:
            entries = [_Entry("http://a.invalid/1", "Edge", _struct_hours_ago(2))]
            # Kept: 2h old, 6h window. mktime would compute 16h and drop it.
            self.assertEqual(len(self._fetch(entries, since_hours=6)), 1)
            # Dropped: 2h old, 1h window.
            self.assertEqual(len(self._fetch(entries, since_hours=1)), 0)
        finally:
            if prev_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = prev_tz
            time.tzset()


# --- 2: render-time filtering ---------------------------------------------


class LatestPerSourceSinceDateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "state.db")

    def tearDown(self):
        self._tmp.cleanup()

    def _add(self, title, published=None, surfaced=None, ready=True):
        url = f"http://example.invalid/{title}"
        self.store.insert_raw(
            "Src", url, title, text="body text",
            surfaced=surfaced, published=published,
        )
        if ready:
            from papernews.store import _url_hash
            self.store.set_summary(_url_hash(url), "a summary")

    def _titles(self, **kwargs):
        return [r["title"] for r in self.store.latest_per_source("Src", 10, **kwargs)]

    def test_without_since_date_returns_all_ready_rows(self):
        self._add("old", published="2020-01-01")
        self._add("new", published="2030-01-01")
        self.assertEqual(sorted(self._titles()), ["new", "old"])

    def test_since_date_excludes_older_articles(self):
        self._add("old", published="2020-01-01")
        self._add("new", published="2030-01-01")
        self.assertEqual(self._titles(since_date="2025-01-01"), ["new"])

    def test_since_date_is_inclusive_of_the_boundary(self):
        self._add("boundary", published="2025-01-01")
        self.assertEqual(self._titles(since_date="2025-01-01"), ["boundary"])

    def test_falls_back_to_surfaced_when_published_is_null(self):
        self._add("surfaced-only", surfaced="2030-01-01")
        self._add("stale-surfaced", surfaced="2020-01-01")
        self.assertEqual(self._titles(since_date="2025-01-01"), ["surfaced-only"])

    def test_undated_articles_are_kept(self):
        """Mirrors the gather-time rule so the two layers agree."""
        self._add("undated")
        self.assertEqual(self._titles(since_date="2025-01-01"), ["undated"])

    def test_unsummarized_rows_are_still_excluded(self):
        self._add("not-ready", published="2030-01-01", ready=False)
        self.assertEqual(self._titles(since_date="2025-01-01"), [])

    def test_limit_still_applies_with_since_date(self):
        for i in range(5):
            self._add(f"a{i}", published=f"2030-01-0{i + 1}")
        rows = self.store.latest_per_source("Src", 2, since_date="2025-01-01")
        self.assertEqual(len(rows), 2)
        # newest first
        self.assertEqual([r["title"] for r in rows], ["a4", "a3"])


# --- 3: the cache key has to notice since_hours ---------------------------


class EditionKeyTests(unittest.TestCase):
    def test_since_hours_changes_the_edition_key(self):
        base = [{"name": "Quanta", "kind": "rss", "limit": 8}]
        windowed = [{"name": "Quanta", "kind": "rss", "limit": 8,
                     "since_hours": 168}]
        self.assertNotEqual(edition_key("t", base), edition_key("t", windowed))

    def test_unset_since_hours_does_not_change_existing_keys(self):
        """Adding the field must not invalidate every cached edition on
        upgrade — a cache miss costs a rebuild plus an LLM call for the
        cover decorations."""
        cfg = [{"name": "Quanta", "kind": "rss", "limit": 8}]
        # The key an existing install's cached PDF was built under.
        self.assertEqual(edition_key("t", cfg), "5a80ef1d3afffd2850d2f905")
        # Explicit None must hash the same as absent.
        self.assertEqual(
            edition_key("t", cfg),
            edition_key("t", [dict(cfg[0], since_hours=None)]),
        )

    def test_differing_since_hours_differ(self):
        a = [{"name": "Q", "kind": "rss", "limit": 8, "since_hours": 24}]
        b = [{"name": "Q", "kind": "rss", "limit": 8, "since_hours": 168}]
        self.assertNotEqual(edition_key("t", a), edition_key("t", b))

    def test_key_is_stable_for_identical_config(self):
        cfg = [{"name": "Q", "kind": "rss", "limit": 8, "since_hours": 24}]
        self.assertEqual(edition_key("t", cfg), edition_key("t", list(cfg)))


# --- 4: Algolia numericFilters encoding -----------------------------------


class FetchHnNumericFiltersTests(unittest.TestCase):
    def test_numeric_filters_are_json_encoded_so_both_survive(self):
        """A bare Python list makes requests emit repeated `numericFilters=`
        params and Algolia honours only the first, silently dropping the
        min_points gate. It has to be a JSON-encoded array."""
        from papernews import fetch

        captured = {}

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"hits": []}

        def _fake_get(url, params=None, timeout=None):
            captured["params"] = params
            return _Resp()

        with mock.patch.object(fetch.requests, "get", _fake_get):
            list(fetch.fetch_hn(limit=10, since_hours=48, min_points=50))

        nf = captured["params"]["numericFilters"]
        self.assertIsInstance(nf, str, "must be a string, not a list")
        decoded = json.loads(nf)
        self.assertEqual(len(decoded), 2)
        self.assertTrue(any(f.startswith("created_at_i>") for f in decoded))
        self.assertIn("points>50", decoded)


if __name__ == "__main__":
    unittest.main()
