from __future__ import annotations

import argparse
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as date_cls, datetime, timedelta, timezone
from pathlib import Path

from .extract import extract
from .fetch import fetch_hn, fetch_rss, fetch_wikipedia_events
from .render import build_pdf
from .store import Store
from .wiki import (
    fetch_did_you_know,
    fetch_quote_of_day,
    fetch_world_news,
    summarize_world_news,
)


def _log(msg: str) -> None:
    sys.stderr.write(msg.rstrip() + "\n")
    sys.stderr.flush()


def _load_sources(path: Path) -> list[dict]:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    return cfg.get("source", [])


# --- stages -----------------------------------------------------------------

def cmd_gather(store: Store, sources: list[dict]) -> int:
    new_count = 0
    failed_count = 0
    for src in sources:
        name = src["name"]
        kind = src.get("kind", "rss")
        limit = src.get("limit", 20)
        _log(f"[gather] {name}")
        try:
            if kind == "hn":
                items = fetch_hn(
                    source_name=name,
                    limit=limit,
                    since_hours=src.get("since_hours", 48),
                    min_points=src.get("min_points", 50),
                )
            elif kind == "rss":
                items = fetch_rss(
                    name, src["url"], limit=limit,
                    since_hours=src.get("since_hours"),
                )
            elif kind == "wikipedia_events":
                items = fetch_wikipedia_events(
                    source_name=name,
                    days_back=src.get("days_back", 1),
                )
            else:
                _log(f"  [warn] unknown source kind '{kind}'")
                continue
        except Exception as e:
            _log(f"  [error] fetch failed: {e}")
            continue

        for it in items:
            if store.exists(it.url, it.title):
                # Back-fill the surfacing date on a re-gather, even if the
                # row already exists.
                store.insert_raw(
                    it.source, it.url, it.title,
                    text=None, surfaced=it.surfaced,
                )
                continue
            try:
                art = extract(it.url, it.title, it.source)
            except Exception as e:
                _log(f"  [error] extract: {it.title[:60]}: {e}")
                store.insert_raw(
                    it.source, it.url, it.title,
                    text=None, surfaced=it.surfaced,
                )
                failed_count += 1
                continue
            if art is None:
                store.insert_raw(
                    it.source, it.url, it.title,
                    text=None, surfaced=it.surfaced,
                )
                failed_count += 1
                _log(f"  - {it.title[:70]}  (no readable content)")
            else:
                # Prefer the article's own date; fall back to the surfacing
                # date so we always have something to display.
                pub = art.published or it.surfaced
                store.insert_raw(
                    it.source, it.url, it.title,
                    text=art.text,
                    surfaced=it.surfaced,
                    published=pub,
                )
                new_count += 1
                _log(f"  + {it.title[:70]}  ({len(art.text)} chars)")
    _log(f"[gather] +{new_count} new, {failed_count} unreadable")
    return 0


_BATCH_SIZE = 8  # articles per LLM call


def _chunks(seq: list, n: int) -> list[list]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def cmd_summarize(store: Store, workers: int) -> int:
    from .summarize import summarize_batch

    pending = store.pending_summary()
    if not pending:
        _log("[summarize] nothing pending")
        return 0
    batches = _chunks(pending, _BATCH_SIZE)
    _log(f"[summarize] {len(pending)} pending in {len(batches)} batch(es) "
         f"of {_BATCH_SIZE} (workers={workers})")

    def _run_batch(rows: list) -> list[tuple[str, str]]:
        items = [(r["title"], r["text"]) for r in rows]
        out = summarize_batch(items)
        return [(rows[i]["url_hash"], out[i]) for i in range(len(rows))]

    done = 0
    errors = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_run_batch, b): b for b in batches}
        for fut in as_completed(futures):
            batch = futures[fut]
            try:
                results = fut.result()
            except Exception as e:
                errors += len(batch)
                _log(f"  [error] batch ({len(batch)} articles): {e}")
                continue
            for h, s in results:
                if s:
                    store.set_summary(h, s)
                    done += 1
                else:
                    errors += 1
            _log(f"  ✓ batch of {len(batch)}")
    _log(f"[summarize] done {done}/{len(pending)}, {errors} errors")
    return 0


def cmd_rewrite(store: Store, workers: int) -> int:
    from .rewrite import rewrite_batch

    pending = store.pending_rewrite()
    if not pending:
        _log("[rewrite] nothing pending")
        return 0
    batches = _chunks(pending, _BATCH_SIZE)
    _log(f"[rewrite] {len(pending)} pending in {len(batches)} batch(es) "
         f"of {_BATCH_SIZE} (workers={workers})")

    def _run_batch(rows: list) -> list[tuple[str, str]]:
        items = [(r["title"], r["text"]) for r in rows]
        out = rewrite_batch(items)
        return [(rows[i]["url_hash"], out[i]) for i in range(len(rows))]

    done = 0
    errors = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_run_batch, b): b for b in batches}
        for fut in as_completed(futures):
            batch = futures[fut]
            try:
                results = fut.result()
            except Exception as e:
                errors += len(batch)
                _log(f"  [error] batch ({len(batch)} articles): {e}")
                continue
            for h, body in results:
                if body:
                    store.set_body(h, body)
                    done += 1
                else:
                    errors += 1
            _log(f"  ✓ batch of {len(batch)}")
    _log(f"[rewrite] done {done}/{len(pending)}, {errors} errors")
    return 0


def _format_date(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%b %d, %Y")
    except ValueError:
        return iso


def _gather_decorations() -> dict:
    """Fetch the cover decorations (Wikipedia world news + QOTD + DYK)."""
    decorations: dict = {}
    try:
        wn = fetch_world_news()
        if wn:
            wn = summarize_world_news(wn)
            decorations["world_news"] = wn
            from datetime import date as _d
            decorations["world_news_date"] = _d.today().strftime("%B %-d, %Y")
    except Exception as e:
        _log(f"  [warn] world news: {e}")
    try:
        qotd = fetch_quote_of_day()
        if qotd:
            decorations["quote"] = {"text": qotd[0], "author": qotd[1]}
    except Exception as e:
        _log(f"  [warn] qotd: {e}")
    try:
        dyk = fetch_did_you_know(limit=4)
        if dyk:
            decorations["dyk"] = dyk
    except Exception as e:
        _log(f"  [warn] dyk: {e}")
    return decorations


def _collect_current_edition(store: Store, sources: list[dict]) -> list[dict]:
    """Pick the latest N articles per source (N = source.limit), in source
    config order. Returns render-ready dicts."""
    out: list[dict] = []
    for src in sources:
        name = src["name"]
        limit = int(src.get("limit", 10))
        since_hours = src.get("since_hours")
        since_date = (
            (datetime.now(timezone.utc) - timedelta(hours=int(since_hours)))
            .date().isoformat()
            if since_hours is not None else None
        )
        rows = store.latest_per_source(name, limit, since_date=since_date)
        for r in rows:
            out.append({
                "source": r["source"],
                "url": r["url"],
                "title": r["title"],
                "text": r["body"] if r["body"] else r["text"],
                "summary": r["summary"],
                "date": _format_date(r["published"] or r["surfaced"]),
            })
    return out


def cmd_render(
    store: Store,
    date: str,
    out_dir: Path,
    sources: list[dict],
) -> int:
    """Build the current edition: latest N per source + live decorations.

    No time-window filter, no read state. PDF reflects whatever is currently
    in the store at this moment.
    """
    articles = _collect_current_edition(store, sources)
    if not articles:
        _log("[render] no ready articles in store yet")
        return 0
    _log("[render] fetching cover decorations (Wikipedia world news + QOTD + DYK)")
    decorations = _gather_decorations()
    _log(f"[render] {len(articles)} articles → PDF")
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = build_pdf(date, articles, out_dir, decorations=decorations)
    print(str(pdf))
    return 0


def cmd_ingest(store: Store, sources: list[dict], workers: int) -> int:
    """Run gather + summarize + rewrite. No PDF — that's the renderer's job."""
    rc = cmd_gather(store, sources)
    if rc:
        return rc
    rc = cmd_summarize(store, workers)
    if rc:
        return rc
    return cmd_rewrite(store, workers)


def cmd_status(store: Store) -> int:
    c = store.counts()
    print(f"total articles         : {c['total']}")
    print(f"  unreadable           : {c['unreadable']}")
    print(f"  awaiting summary     : {c['pending_summary']}")
    print(f"  awaiting rewrite     : {c['pending_rewrite']}")
    print(f"  awaiting render      : {c['pending_render']}")
    print(f"  already rendered     : {c['rendered']}")
    return 0


# --- CLI --------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="papernews")
    p.add_argument("--config", type=Path, default=Path("sources.toml"))
    p.add_argument("--out",    type=Path, default=Path("archive"))
    p.add_argument("--state",  type=Path, default=Path("state.db"))

    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("gather", help="fetch + extract new articles into the store")

    sp_sum = sub.add_parser("summarize", help="summarize articles still missing a summary")
    sp_sum.add_argument("--workers", type=int, default=6)

    sp_rw = sub.add_parser("rewrite", help="reformat article bodies into clean paragraphs")
    sp_rw.add_argument("--workers", type=int, default=6)

    sp_ing = sub.add_parser("ingest", help="gather + summarize + rewrite (no PDF)")
    sp_ing.add_argument("--workers", type=int, default=6)

    sp_ren = sub.add_parser("render", help="render the current edition PDF")
    sp_ren.add_argument("--date", default=date_cls.today().isoformat())

    sub.add_parser("status", help="print store counts")

    sp_b = sub.add_parser("build", help="ingest + render (default)")
    sp_b.add_argument("--workers", type=int, default=6)
    sp_b.add_argument("--date",    default=date_cls.today().isoformat())

    args = p.parse_args(argv)
    cmd = args.cmd or "build"

    if not args.config.exists():
        _log(f"[fatal] config not found: {args.config}")
        return 2

    # Always load config (cheap; renderer needs source order).
    sources = _load_sources(args.config)
    if cmd in ("gather", "ingest", "build") and not sources:
        _log("[fatal] no sources configured")
        return 2

    store = Store(args.state)

    if cmd == "gather":
        return cmd_gather(store, sources)
    if cmd == "summarize":
        return cmd_summarize(store, args.workers)
    if cmd == "rewrite":
        return cmd_rewrite(store, args.workers)
    if cmd == "ingest":
        return cmd_ingest(store, sources, args.workers)
    if cmd == "render":
        return cmd_render(store, args.date, args.out, sources)
    if cmd == "status":
        return cmd_status(store)
    if cmd == "build":
        rc = cmd_ingest(store, sources, args.workers)
        if rc:
            return rc
        return cmd_render(store, args.date, args.out, sources)

    _log(f"[fatal] unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
