"""Flask web service for papernews.

Routes:
  GET /              landing page (cover preview + 'Read today' link)
  GET /digest.pdf    current edition PDF (cached, built on demand)
  GET /preview.png   page-1 PNG of the current edition
  GET /sources       JSON list of configured sources + counts
  GET /healthz       liveness probe

Background:
  APScheduler runs `ingest` every INGEST_INTERVAL_SECONDS (default 4h).

Environment:
  PAPERNEWS_STATE        path to state.db          (default: state.db)
  PAPERNEWS_CONFIG       path to sources.toml      (default: sources.toml)
  PAPERNEWS_CACHE        path to cache dir         (default: archive/cache)
  PAPERNEWS_WORKERS      LLM workers               (default: 8)

  Scheduling — pick one:
    INGEST_INTERVAL_SECONDS    every N seconds         (default: 14400 = 4h)
    INGEST_SCHEDULE            "HH:MM,HH:MM,..." cron-style fixed times
    INGEST_TIMEZONE            IANA tz, used with INGEST_SCHEDULE (default: UTC)

  Post-ingest delivery hook:
    POST_INGEST_HOOK           executable on disk; receives the PDF path as $1
    POST_INGEST_HOOK_TIMEOUT   seconds (default: 300)

  ANTHROPIC_API_KEY      required for the Claude SDK
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import tomllib
from datetime import date
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, abort, jsonify, redirect, send_file, request

from .cache import edition_key, ensure_dir, epub_path, pdf_path, preview_path
from .cli import (
    _collect_current_edition,
    _gather_decorations,
    cmd_ingest,
)
from .preview import render_cover_png
from .render import build_epub, build_pdf
from .store import Store


# --- Config helpers -------------------------------------------------------

def _cfg_path(env_var: str, default: str) -> Path:
    return Path(os.environ.get(env_var, default))


STATE_PATH      = _cfg_path("PAPERNEWS_STATE",  "state.db")
CONFIG_PATH     = _cfg_path("PAPERNEWS_CONFIG", "sources.toml")
CACHE_DIR       = _cfg_path("PAPERNEWS_CACHE",  "archive/cache")
WORKERS         = int(os.environ.get("PAPERNEWS_WORKERS", "8"))
INGEST_EVERY    = int(os.environ.get("INGEST_INTERVAL_SECONDS", str(4 * 3600)))
OUTPUT_FORMATS  = {f.strip().lower() for f in os.environ.get("OUTPUT_FORMATS", "pdf").split(",")}


def _load_sources() -> list[dict]:
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f).get("source", [])


# --- Build pipeline -------------------------------------------------------

# Per-key lock so concurrent requests for the same cache key only build once.
_build_locks: dict[str, threading.Lock] = {}
_build_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _build_locks_guard:
        lock = _build_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _build_locks[key] = lock
        return lock


def _current_key(store: Store, sources: list[dict]) -> str:
    return edition_key(store.max_fetched_at(), sources)


def _build_pdf_for_key(key: str, store: Store, sources: list[dict]) -> Path:
    """Build the current-edition PDF into the cache, keyed by `key`."""
    out = pdf_path(CACHE_DIR, key)
    if out.exists():
        return out
    with _lock_for(key):
        if out.exists():
            return out
        ensure_dir(CACHE_DIR)
        articles = _collect_current_edition(store, sources)
        decorations = _gather_decorations()
        # Use the cache dir as build workdir so .build/ stays beside the PDF.
        tmp_pdf = build_pdf(
            date.today().isoformat(),
            articles,
            CACHE_DIR,
            decorations=decorations,
        )
        if tmp_pdf != out:
            tmp_pdf.replace(out)
    return out


def _build_epub_for_key(key: str, store: Store, sources: list[dict]) -> Path:
    """Build the current-edition EPUB into the cache, keyed by `key`."""
    out = epub_path(CACHE_DIR, key)
    if out.exists():
        return out
    with _lock_for(f"epub:{key}"):
        if out.exists():
            return out
        ensure_dir(CACHE_DIR)
        articles = _collect_current_edition(store, sources)
        decorations = _gather_decorations()
        tmp_epub = build_epub(
            date.today().isoformat(),
            articles,
            CACHE_DIR,
            decorations=decorations,
        )
        if tmp_epub != out:
            tmp_epub.replace(out)
    return out


def _build_preview_for_key(key: str, pdf: Path) -> Path:
    out = preview_path(CACHE_DIR, key)
    if out.exists():
        return out
    with _lock_for(f"preview:{key}"):
        if out.exists():
            return out
        render_cover_png(pdf, out, dpi=180)
    return out


# --- Background ingest ----------------------------------------------------

_ingest_lock = threading.Lock()


def _do_ingest() -> None:
    if not _ingest_lock.acquire(blocking=False):
        return  # one ingest at a time
    try:
        sources = _load_sources()
        store = Store(STATE_PATH)
        cmd_ingest(store, sources, WORKERS)

        # Optional post-ingest delivery hook. The hook is an executable on the
        # container's filesystem (usually dropped in via the bind volume) that
        # receives the freshly-built PDF path as its single argument. Useful
        # for SCP-ing to a reMarkable, mailing it somewhere, printing, etc.
        hook = os.environ.get("POST_INGEST_HOOK", "").strip()
        if hook:
            hook_timeout = int(os.environ.get("POST_INGEST_HOOK_TIMEOUT", "300"))
            key = _current_key(store, sources)
            for fmt, builder in [
                ("pdf", lambda: _build_pdf_for_key(key, store, sources)),
                ("epub", lambda: _build_epub_for_key(key, store, sources)),
            ]:
                if fmt not in OUTPUT_FORMATS:
                    continue
                try:
                    out_file = builder()
                    subprocess.run([hook, str(out_file)], timeout=hook_timeout, check=False)
                except Exception as e:
                    sys.stderr.write(f"[post-ingest hook/{fmt}] {e}\n")
                    sys.stderr.flush()
    finally:
        _ingest_lock.release()


# --- Flask app ------------------------------------------------------------

def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)

    @app.get("/healthz")
    def healthz():
        return "ok", 200

    @app.get("/")
    def index():
        epub_link = (
            '<p><a class="cta" href="/digest.epub">Download today (EPUB)</a></p>'
            if "epub" in OUTPUT_FORMATS else ""
        )
        html = _LANDING_HTML.replace("((epub_link))", epub_link)
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}

    @app.get("/sources")
    def sources_endpoint():
        sources = _load_sources()
        store = Store(STATE_PATH)
        return jsonify({
            "sources": [
                {"name": s["name"], "kind": s.get("kind"), "limit": s.get("limit")}
                for s in sources
            ],
            "max_fetched_at": store.max_fetched_at(),
        })

    @app.get("/digest.pdf")
    def digest_pdf():
        sources = _load_sources()
        store = Store(STATE_PATH)
        key = _current_key(store, sources)
        pdf = _build_pdf_for_key(key, store, sources)
        return send_file(
            pdf,
            mimetype="application/pdf",
            as_attachment=False,
            download_name=f"papernews-{date.today().isoformat()}.pdf",
            max_age=300,
        )

    @app.get("/digest.epub")
    def digest_epub():
        if "epub" not in OUTPUT_FORMATS:
            abort(404)
        sources = _load_sources()
        store = Store(STATE_PATH)
        key = _current_key(store, sources)
        epub = _build_epub_for_key(key, store, sources)
        return send_file(
            epub,
            mimetype="application/epub+zip",
            as_attachment=True,
            download_name=f"papernews-{date.today().isoformat()}.epub",
            max_age=300,
        )

    @app.get("/preview.png")
    def preview_png():
        sources = _load_sources()
        store = Store(STATE_PATH)
        key = _current_key(store, sources)
        pdf = _build_pdf_for_key(key, store, sources)
        png = _build_preview_for_key(key, pdf)
        return send_file(png, mimetype="image/png", max_age=300)

    @app.post("/ingest")
    def trigger_ingest():
        # Optional manual kick; for cron-style external triggers.
        if _ingest_lock.locked():
            return jsonify({"status": "already running"}), 202
        threading.Thread(target=_do_ingest, daemon=True).start()
        return jsonify({"status": "started"}), 202

    @app.get("/ingest")
    def ingest_get_hint():
        # Friendly 405 — easier than rediscovering you wanted POST.
        return (
            jsonify({
                "error": "POST required to trigger ingest",
                "hint": "curl -X POST http://localhost:8000/ingest",
                "note": "the background scheduler also runs ingest automatically",
            }),
            405,
        )

    return app


def start_scheduler() -> BackgroundScheduler:
    """Start the background ingest scheduler.

    Two modes (in priority order):
      INGEST_SCHEDULE=07:00,18:00   → cron-style at the listed HH:MM times
      INGEST_INTERVAL_SECONDS=14400 → every N seconds (default 4h)

    The cron mode also honours INGEST_TIMEZONE (an IANA tz, default UTC).
    """
    sched = BackgroundScheduler(daemon=True)
    schedule = os.environ.get("INGEST_SCHEDULE", "").strip()
    if schedule:
        tz = os.environ.get("INGEST_TIMEZONE", "UTC")
        for i, hm in enumerate(s.strip() for s in schedule.split(",") if s.strip()):
            try:
                h, m = hm.split(":")
                sched.add_job(
                    _do_ingest, "cron",
                    hour=int(h), minute=int(m),
                    id=f"ingest_cron_{i}",
                    timezone=tz,
                )
            except (ValueError, KeyError):
                sys.stderr.write(f"[scheduler] ignoring invalid time: {hm!r}\n")
                sys.stderr.flush()
    else:
        sched.add_job(_do_ingest, "interval",
                      seconds=INGEST_EVERY, id="ingest",
                      next_run_time=None)
    sched.start()
    return sched


_LANDING_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>papernews</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    body { font-family: Georgia, "Times New Roman", serif; max-width: 720px;
           margin: 4rem auto; padding: 0 1.25rem; color: #222; }
    h1   { font-size: 2.4rem; margin: 0 0 0.2rem; }
    .sub { color: #777; margin: 0 0 2rem; font-size: 1rem; }
    a.cta { display: inline-block; padding: 0.7rem 1.4rem; border: 1px solid #222;
            text-decoration: none; color: #222; font-weight: bold; margin-top: 1rem;}
    a.cta:hover { background: #222; color: #fff; }
    img.cover { width: 100%; height: auto; border: 1px solid #eee;
                box-shadow: 0 2px 10px rgba(0,0,0,0.08); }
    .meta { color: #999; font-size: 0.85rem; margin-top: 3rem; }
  </style>
</head>
<body>
  <h1>papernews</h1>
  <p class="sub">A curated PDF you read on your reMarkable, not in a browser.</p>
  <img class="cover" src="/preview.png" alt="Cover preview">
  <p><a class="cta" href="/digest.pdf">Read today (PDF)</a></p>
  ((epub_link))
  <p class="meta">Updated automatically every few hours. <a href="/sources">Sources</a>.</p>
</body>
</html>
"""


# WSGI entry point
app = create_app()
_scheduler = start_scheduler() if os.environ.get("PAPERNEWS_NO_SCHED") != "1" else None
