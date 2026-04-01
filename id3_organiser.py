#!/usr/bin/env python3
"""
ID3 Music Organiser
───────────────────
Scans music files, extracts metadata, calculates an organised folder structure,
and provides a web UI for review and triggering file moves.

Usage:
    python id3_organiser.py /path/to/music
    python id3_organiser.py /path/to/music --processed /output/processed --unmatched /output/unmatched
"""

import csv
import io
import json
import os
import queue
import re
import shutil
import sqlite3
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

import click
from enrichment.pipeline import EnrichmentScheduler
from watchdog.events import FileSystemEventHandler
from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from mutagen import File as MutagenFile
from rich import box
from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

# ─────────────────────────────────────────────────────────────────────────────
# Constants & globals
# ─────────────────────────────────────────────────────────────────────────────

VERSION = "1.0.0"
SUPPORTED_EXTENSIONS = {".mp3", ".flac", ".aac", ".m4a"}

SOURCE_DIR     = "/input"
PROCESSED_BASE = "/destination"
UNMATCHED_BASE = "/destination/_unmatched"

console = Console()
app = Flask(__name__)

# Populated at CLI startup; read by Flask routes
_config: dict = {}
# Per-move SSE event queue (reset each time a move is triggered)
_move_event_queue: queue.Queue = queue.Queue()
# Enrichment scheduler (set when enrich command runs)
_enrichment_scheduler: "EnrichmentScheduler | None" = None

# ─────────────────────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id               INTEGER  PRIMARY KEY AUTOINCREMENT,
    original_path    TEXT     NOT NULL UNIQUE,
    filename         TEXT     NOT NULL,
    artist           TEXT,
    album            TEXT,
    track_number     TEXT,
    title            TEXT,
    genre            TEXT,
    year             TEXT,
    duration         REAL,
    destination_path TEXT,
    matched          INTEGER  DEFAULT 0,
    status           TEXT     DEFAULT 'pending',
    error_message    TEXT,
    scanned_at       TEXT,
    moved_at         TEXT,
    enrichment_status TEXT    DEFAULT 'none',
    tags_written_at  TEXT
);

CREATE TABLE IF NOT EXISTS enrichment_candidates (
    id                INTEGER  PRIMARY KEY AUTOINCREMENT,
    track_id          INTEGER  NOT NULL REFERENCES tracks(id),
    source            TEXT     NOT NULL,
    suggested_artist  TEXT,
    suggested_album   TEXT,
    suggested_title   TEXT,
    suggested_year    TEXT,
    suggested_genre   TEXT,
    confidence        TEXT     NOT NULL,
    status            TEXT     NOT NULL DEFAULT 'pending_review',
    raw_response      TEXT,
    created_at        TEXT     NOT NULL
);

CREATE TABLE IF NOT EXISTS enrichment_runs (
    id                      INTEGER  PRIMARY KEY AUTOINCREMENT,
    started_at              TEXT     NOT NULL,
    last_processed_track_id INTEGER,
    tracks_processed        INTEGER  DEFAULT 0,
    tracks_remaining        INTEGER,
    daily_limit             INTEGER  NOT NULL,
    status                  TEXT     NOT NULL DEFAULT 'running',
    claude_batch_id         TEXT
);
"""


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_config["db_path"])
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db() -> None:
    with _get_db() as conn:
        conn.executescript(_SCHEMA)


def _migrate_db() -> None:
    """Idempotently add new columns to existing tables."""
    with _get_db() as conn:
        existing_tracks = {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
        if "enrichment_status" not in existing_tracks:
            conn.execute(
                "ALTER TABLE tracks ADD COLUMN enrichment_status TEXT DEFAULT 'none'"
            )
        if "tags_written_at" not in existing_tracks:
            conn.execute("ALTER TABLE tracks ADD COLUMN tags_written_at TEXT")

        existing_runs = {row[1] for row in conn.execute("PRAGMA table_info(enrichment_runs)")}
        if "claude_batch_id" not in existing_runs:
            conn.execute("ALTER TABLE enrichment_runs ADD COLUMN claude_batch_id TEXT")

        # Clear placeholder "Unknown Artist" / "Unknown Album" values that ripping
        # software writes into tags, and mark affected tracks for enrichment.
        # TRIM() in SQLite only strips spaces; REPLACE handles tabs/other whitespace.
        conn.execute("""
            UPDATE tracks
            SET artist            = NULL,
                matched           = 0,
                enrichment_status = CASE WHEN enrichment_status != 'in_progress'
                                         THEN 'none' ELSE enrichment_status END
            WHERE TRIM(REPLACE(REPLACE(LOWER(artist), CHAR(9), ' '), CHAR(10), ' '))
                  = 'unknown artist'
        """)
        conn.execute("""
            UPDATE tracks
            SET album             = NULL,
                matched           = 0,
                enrichment_status = CASE WHEN enrichment_status != 'in_progress'
                                         THEN 'none' ELSE enrichment_status END
            WHERE TRIM(REPLACE(REPLACE(LOWER(album), CHAR(9), ' '), CHAR(10), ' '))
                  = 'unknown album'
        """)


# ─────────────────────────────────────────────────────────────────────────────
# Metadata helpers
# ─────────────────────────────────────────────────────────────────────────────

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize(name: str | None, fallback: str = "Unknown") -> str:
    if not name:
        return fallback
    name = _UNSAFE_CHARS.sub("_", name).strip(". ")
    return name or fallback


def _parse_track_number(raw: str | None) -> str | None:
    """'3', '03', '3/12'  →  '03'.  Returns None if unparseable."""
    if not raw:
        return None
    m = re.match(r"(\d+)", str(raw))
    return f"{int(m.group(1)):02d}" if m else None


_UNKNOWN_PLACEHOLDERS = {"unknown artist", "unknown album"}


def _extract_metadata(file_path: str) -> dict:
    """Return a normalised tag dict for a music file (easy=True flattens formats)."""
    try:
        audio = MutagenFile(file_path, easy=True)
    except Exception:
        return {}
    if audio is None:
        return {}

    def tag(key: str) -> str | None:
        val = audio.get(key)
        return str(val[0]).strip() if val else None

    def clean(value: str | None) -> str | None:
        """Return None for known ripping-software placeholder strings."""
        if value and value.strip().lower() in _UNKNOWN_PLACEHOLDERS:
            return None
        return value

    meta = {
        "title":        tag("title"),
        "artist":       clean(tag("artist") or tag("albumartist")),
        "album":        clean(tag("album")),
        "track_number": tag("tracknumber"),
        "genre":        tag("genre"),
        "year":         tag("date"),
    }
    try:
        meta["duration"] = audio.info.length
    except Exception:
        meta["duration"] = None
    return meta


def _calculate_destination(meta: dict, original_path: str) -> tuple[str, bool]:
    """
    Return (destination_path, is_matched).
    matched   → /destination/<Artist>/<Album>/<NN>. <Title>.ext
    unmatched → /destination/_unmatched/<relative-original-path>
    """
    artist = meta.get("artist")
    album  = meta.get("album")
    title  = meta.get("title")
    ext    = Path(original_path).suffix.lower()

    if artist and album and title:
        tn = _parse_track_number(meta.get("track_number"))
        prefix = f"{tn}. " if tn else ""
        filename = _sanitize(f"{prefix}{title}") + ext
        dest = (
            Path(PROCESSED_BASE)
            / _sanitize(artist)
            / _sanitize(album)
            / filename
        )
        return str(dest), True
    else:
        try:
            rel = Path(original_path).relative_to(SOURCE_DIR)
        except ValueError:
            rel = Path(Path(original_path).name)
        return str(Path(UNMATCHED_BASE) / rel), False


# ─────────────────────────────────────────────────────────────────────────────
# CLI Phases
# ─────────────────────────────────────────────────────────────────────────────

def _phase1_collect_files() -> list[str]:
    """Walk source_dir and return all supported music file paths."""
    found: list[str] = []
    with Progress(
        SpinnerColumn(style="bold cyan"),
        TextColumn(
            "[bold cyan]Phase 1 / 3[/bold cyan]  [dim]—[/dim]  Scanning for music files…"
        ),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("scan", total=None)
        for root, _dirs, files in os.walk(SOURCE_DIR):
            for fname in files:
                if Path(fname).suffix.lower() in SUPPORTED_EXTENSIONS:
                    found.append(os.path.join(root, fname))

    console.print(
        f"  [bold green]✓[/bold green]  Found "
        f"[bold white]{len(found)}[/bold white] music files"
        f"  [dim]({SOURCE_DIR})[/dim]"
    )
    return found


def _phase2_extract_and_store(files: list[str]) -> tuple[int, int]:
    """Extract metadata for each file and persist to DB. Returns (total, matched)."""
    matched = 0
    now = datetime.now().isoformat()

    with Progress(
        SpinnerColumn(style="bold magenta"),
        TextColumn(
            "[bold magenta]Phase 2 / 3[/bold magenta]  [dim]—[/dim]  Extracting metadata…"
        ),
        BarColumn(bar_width=44, style="magenta", complete_style="bold magenta"),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("extract", total=len(files))

        with _get_db() as conn:
            for fpath in files:
                meta = _extract_metadata(fpath)
                dest, is_matched = _calculate_destination(meta, fpath)
                if is_matched:
                    matched += 1
                conn.execute(
                    """
                    INSERT OR REPLACE INTO tracks
                        (original_path, filename, artist, album, track_number,
                         title, genre, year, duration, destination_path,
                         matched, status, scanned_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        fpath,
                        Path(fpath).name,
                        meta.get("artist"),
                        meta.get("album"),
                        meta.get("track_number"),
                        meta.get("title"),
                        meta.get("genre"),
                        meta.get("year"),
                        meta.get("duration"),
                        dest,
                        1 if is_matched else 0,
                        now,
                    ),
                )
                progress.advance(task)

    unmatched = len(files) - matched
    console.print(
        f"  [bold green]✓[/bold green]  Processed [bold white]{len(files)}[/bold white] tracks  "
        f"[green]{matched}[/green] matched  ·  [yellow]{unmatched}[/yellow] unmatched"
    )
    return len(files), matched


def _phase3_summary(total: int, matched: int) -> None:
    """Render the Phase 3 destination summary panel."""
    unmatched = total - matched
    tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    tbl.add_column(justify="right", style="dim")
    tbl.add_column()
    tbl.add_row("Total",     f"[bold white]{total}[/bold white]")
    tbl.add_row(
        "Matched",
        f"[bold green]{matched}[/bold green]  "
        f"[dim]→  {PROCESSED_BASE}[/dim]",
    )
    tbl.add_row(
        "Unmatched",
        f"[bold yellow]{unmatched}[/bold yellow]  "
        f"[dim]→  {UNMATCHED_BASE}[/dim]",
    )

    console.print()
    console.print(
        Panel(
            tbl,
            title="[bold]Phase 3 / 3  —  Destination paths calculated[/bold]",
            border_style="#7c3aed",
            padding=(1, 2),
        )
    )


def _ingest_file(fpath: str) -> None:
    """Extract metadata from a single file and upsert into DB."""
    meta = _extract_metadata(fpath)
    dest, is_matched = _calculate_destination(meta, fpath)
    now = datetime.now().isoformat()
    with _get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO tracks
                (original_path, filename, artist, album, track_number,
                 title, genre, year, duration, destination_path,
                 matched, status, scanned_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                fpath,
                Path(fpath).name,
                meta.get("artist"),
                meta.get("album"),
                meta.get("track_number"),
                meta.get("title"),
                meta.get("genre"),
                meta.get("year"),
                meta.get("duration"),
                dest,
                1 if is_matched else 0,
                now,
            ),
        )
    console.print(
        f"  [cyan]+ Watcher:[/cyan] {Path(fpath).name}"
        f"  [dim]({'matched' if is_matched else 'needs enrichment'})[/dim]"
    )


class MusicFileHandler(FileSystemEventHandler):
    def on_created(self, event):
        if (not event.is_directory
                and Path(event.src_path).suffix.lower() in SUPPORTED_EXTENSIONS):
            _ingest_file(event.src_path)

    def on_moved(self, event):
        if (not event.is_directory
                and Path(event.dest_path).suffix.lower() in SUPPORTED_EXTENSIONS):
            _ingest_file(event.dest_path)


def _start_watcher(use_polling: bool, interval: int) -> None:
    from watchdog.observers import Observer
    from watchdog.observers.polling import PollingObserver

    ObserverClass = PollingObserver if use_polling else Observer
    kwargs = {"timeout": interval} if use_polling else {}
    observer = ObserverClass(**kwargs)
    observer.schedule(MusicFileHandler(), SOURCE_DIR, recursive=True)
    observer.daemon = True
    observer.start()
    mode = f"polling every {interval}s" if use_polling else "inotify"
    console.print(f"[dim]Directory watcher started ({mode}) on {SOURCE_DIR}[/dim]")


# ─────────────────────────────────────────────────────────────────────────────
# Flask API
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/tracks")
def api_tracks():
    status = request.args.get("status", "all")
    match  = request.args.get("match",  "all")
    search = request.args.get("q", "").strip()
    page   = max(1, int(request.args.get("page", 1)))
    per    = min(200, max(10, int(request.args.get("per", 50))))

    sql    = "SELECT * FROM tracks WHERE 1=1"
    params: list = []

    if status != "all":
        sql += " AND status = ?";  params.append(status)
    if match == "matched":
        sql += " AND matched = 1"
    elif match == "unmatched":
        sql += " AND matched = 0"
    enrich_filter = request.args.get("enrichment", "all")
    if enrich_filter != "all":
        sql += " AND enrichment_status = ?"; params.append(enrich_filter)
    if search:
        like = f"%{search}%"
        sql += " AND (artist LIKE ? OR album LIKE ? OR title LIKE ? OR filename LIKE ?)"
        params.extend([like, like, like, like])

    sql += " ORDER BY artist NULLS LAST, album NULLS LAST, track_number, title"

    with _get_db() as conn:
        total_rows = conn.execute(
            f"SELECT COUNT(*) FROM ({sql})", params
        ).fetchone()[0]
        rows = conn.execute(
            sql + f" LIMIT {per} OFFSET {(page - 1) * per}", params
        ).fetchall()

    return jsonify({"total": total_rows, "page": page, "per": per,
                    "tracks": [dict(r) for r in rows]})


@app.route("/api/tracks/<int:track_id>", methods=["PATCH"])
def api_update_track(track_id: int):
    data    = request.json or {}
    allowed = {"destination_path", "artist", "album", "track_number", "title"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({"error": "nothing to update"}), 400

    set_sql = ", ".join(f"{k} = ?" for k in updates)
    values  = list(updates.values()) + [track_id]
    with _get_db() as conn:
        conn.execute(f"UPDATE tracks SET {set_sql} WHERE id = ?", values)
    return jsonify({"ok": True})


@app.route("/api/stats")
def api_stats():
    with _get_db() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*)                                      AS total,
                SUM(matched)                                  AS matched,
                SUM(CASE WHEN matched = 0  THEN 1 ELSE 0 END) AS unmatched,
                SUM(CASE WHEN status = 'moved'   THEN 1 ELSE 0 END) AS moved,
                SUM(CASE WHEN status = 'error'   THEN 1 ELSE 0 END) AS errors,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending
            FROM tracks
        """).fetchone()
    return jsonify(dict(row))


@app.route("/api/export/csv")
def api_export_csv():
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT filename, artist, album, track_number, title, genre, year, "
            "duration, original_path, destination_path FROM tracks "
            "ORDER BY artist NULLS LAST, album NULLS LAST, track_number, title"
        ).fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Filename", "Artist", "Album", "Track Number",
        "Title", "Genre", "Year", "Duration",
        "Original Path", "Destination Path",
    ])
    for row in rows:
        writer.writerow([v if v is not None else "" for v in row])

    return app.response_class(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="music_library.csv"'},
    )


@app.route("/api/move", methods=["POST"])
def api_move():
    global _move_event_queue
    _move_event_queue = queue.Queue()

    data      = request.json or {}
    track_ids = data.get("track_ids")  # None / [] → all pending

    with _get_db() as conn:
        if track_ids:
            ph   = ",".join("?" * len(track_ids))
            rows = conn.execute(
                f"SELECT * FROM tracks WHERE id IN ({ph}) AND status = 'pending'",
                track_ids,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tracks WHERE status = 'pending'"
            ).fetchall()

    tracks = [dict(r) for r in rows]

    def _do_move():
        moved = failed = 0
        for i, track in enumerate(tracks, 1):
            src = track["original_path"]
            dst = track["destination_path"]
            tid = track["id"]

            _move_event_queue.put({
                "type":     "progress",
                "id":       tid,
                "current":  i,
                "total":    len(tracks),
                "filename": track["filename"],
                "status":   "moving",
            })

            try:
                if not os.path.exists(src):
                    raise FileNotFoundError(f"Source gone: {src}")

                dst_path = Path(dst)
                dst_path.parent.mkdir(parents=True, exist_ok=True)

                # Avoid overwriting existing files
                final = dst_path
                n = 1
                while final.exists():
                    final = dst_path.with_stem(f"{dst_path.stem}_{n}")
                    n += 1

                shutil.move(src, str(final))
                moved += 1

                with _get_db() as conn:
                    conn.execute(
                        "UPDATE tracks SET status='moved', moved_at=?, destination_path=? WHERE id=?",
                        (datetime.now().isoformat(), str(final), tid),
                    )
                _move_event_queue.put({
                    "type":        "progress",
                    "id":          tid,
                    "current":     i,
                    "total":       len(tracks),
                    "filename":    track["filename"],
                    "status":      "moved",
                    "destination": str(final),
                })

            except Exception as exc:
                failed += 1
                err = str(exc)
                with _get_db() as conn:
                    conn.execute(
                        "UPDATE tracks SET status='error', error_message=? WHERE id=?",
                        (err, tid),
                    )
                _move_event_queue.put({
                    "type":     "progress",
                    "id":       tid,
                    "current":  i,
                    "total":    len(tracks),
                    "filename": track["filename"],
                    "status":   "error",
                    "message":  err,
                })

        _move_event_queue.put({
            "type":   "complete",
            "moved":  moved,
            "failed": failed,
            "total":  len(tracks),
        })

    threading.Thread(target=_do_move, daemon=True).start()
    return jsonify({"status": "started", "total": len(tracks)})


@app.route("/api/move/stream")
def api_move_stream():
    def _generate():
        while True:
            try:
                event = _move_event_queue.get(timeout=60)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "complete":
                    break
            except queue.Empty:
                yield 'data: {"type":"heartbeat"}\n\n'

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Enrichment API
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/enrich/candidates")
def api_enrich_candidates():
    status     = request.args.get("status", "pending_review")
    source     = request.args.get("source", "all")
    confidence = request.args.get("confidence", "all")
    page       = max(1, int(request.args.get("page", 1)))
    per        = min(200, max(10, int(request.args.get("per", 50))))

    sql    = """
        SELECT ec.*, t.original_path, t.filename
        FROM enrichment_candidates ec
        JOIN tracks t ON t.id = ec.track_id
        WHERE 1=1
    """
    params: list = []

    if status != "all":
        sql += " AND ec.status = ?";     params.append(status)
    if source != "all":
        sql += " AND ec.source = ?";     params.append(source)
    if confidence != "all":
        sql += " AND ec.confidence = ?"; params.append(confidence)

    sql += " ORDER BY ec.confidence DESC, ec.created_at DESC"

    with _get_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
        rows  = conn.execute(
            sql + f" LIMIT {per} OFFSET {(page - 1) * per}", params
        ).fetchall()

    return jsonify({"total": total, "page": page, "per": per,
                    "candidates": [dict(r) for r in rows]})


@app.route("/api/enrich/candidates/<int:candidate_id>", methods=["PATCH"])
def api_enrich_candidate_update(candidate_id: int):
    data    = request.json or {}
    allowed = {"status", "suggested_artist", "suggested_album",
               "suggested_title", "suggested_year"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({"error": "nothing to update"}), 400
    set_sql = ", ".join(f"{k} = ?" for k in updates)
    values  = list(updates.values()) + [candidate_id]
    with _get_db() as conn:
        conn.execute(
            f"UPDATE enrichment_candidates SET {set_sql} WHERE id = ?", values
        )
    return jsonify({"ok": True})


@app.route("/api/enrich/status")
def api_enrich_status():
    if _enrichment_scheduler is not None:
        return jsonify(_enrichment_scheduler.get_status())

    with _get_db() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE matched=0 "
            "AND enrichment_status NOT IN ('enriched')"
        ).fetchone()[0]
    return jsonify({
        "running": False,
        "tracks_processed_today": 0,
        "tracks_remaining": remaining,
        "next_run": "not started",
        "last_run": None,
    })


@app.route("/api/enrich/run-now", methods=["POST"])
def api_enrich_run_now():
    if _enrichment_scheduler is not None:
        _enrichment_scheduler.run_now()
        return jsonify({"ok": True, "message": "batch triggered"})
    return jsonify({"ok": False, "message": "enrichment scheduler not running"}), 400


_apply_event_queue: queue.Queue = queue.Queue()


@app.route("/api/enrich/apply", methods=["POST"])
def api_enrich_apply():
    global _apply_event_queue
    _apply_event_queue = queue.Queue()

    from enrichment.tag_writer import write_tags, TagWriteError

    data          = request.json or {}
    candidate_ids = data.get("candidate_ids", [])
    if not candidate_ids:
        return jsonify({"error": "no candidate_ids provided"}), 400

    with _get_db() as conn:
        ph   = ",".join("?" * len(candidate_ids))
        rows = conn.execute(
            f"SELECT ec.*, t.original_path, t.filename FROM enrichment_candidates ec "
            f"JOIN tracks t ON t.id = ec.track_id "
            f"WHERE ec.id IN ({ph}) AND ec.status = 'pending_review'",
            candidate_ids,
        ).fetchall()
    candidates = [dict(r) for r in rows]

    def _do_apply():
        applied = failed = 0
        for i, cand in enumerate(candidates, 1):
            tags = {
                "artist":       cand["suggested_artist"],
                "album":        cand["suggested_album"],
                "title":        cand["suggested_title"],
                "year":         cand["suggested_year"],
                "genre":        cand.get("suggested_genre"),
            }
            tid  = cand["track_id"]
            cid  = cand["id"]
            src  = cand["original_path"]

            _apply_event_queue.put({
                "type": "progress", "id": cid, "current": i,
                "total": len(candidates), "filename": cand.get("filename", ""),
                "status": "applying",
            })

            try:
                write_tags(src, tags)
                meta = {**tags, "track_number": None}
                dest, is_matched = _calculate_destination(meta, src)
                now = datetime.now().isoformat()
                with _get_db() as conn:
                    conn.execute(
                        "UPDATE tracks SET artist=?, album=?, title=?, year=?, "
                        "destination_path=?, matched=?, enrichment_status='enriched', "
                        "tags_written_at=? WHERE id=?",
                        (tags["artist"], tags["album"], tags["title"], tags.get("year"),
                         dest, 1 if is_matched else 0, now, tid)
                    )
                    conn.execute(
                        "UPDATE enrichment_candidates SET status='applied' WHERE id=?", (cid,)
                    )
                applied += 1
                _apply_event_queue.put({
                    "type": "progress", "id": cid, "current": i,
                    "total": len(candidates), "status": "applied",
                })
            except Exception as exc:
                failed += 1
                with _get_db() as conn:
                    conn.execute(
                        "UPDATE tracks SET error_message=? WHERE id=?", (str(exc), tid)
                    )
                _apply_event_queue.put({
                    "type": "progress", "id": cid, "current": i,
                    "total": len(candidates), "status": "error", "message": str(exc),
                })

        _apply_event_queue.put({"type": "complete", "applied": applied, "failed": failed})

    threading.Thread(target=_do_apply, daemon=True).start()
    return jsonify({"status": "started", "total": len(candidates)})


@app.route("/api/enrich/apply/stream")
def api_enrich_apply_stream():
    def _generate():
        while True:
            try:
                event = _apply_event_queue.get(timeout=60)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "complete":
                    break
            except queue.Empty:
                yield 'data: {"type":"heartbeat"}\n\n'

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--port",           default=5000,                show_default=True,
              help="Web server port.")
@click.option("--db",             default="music_organiser.db", show_default=True,
              help="SQLite database path.")
@click.option("--daily-limit",    default=500,                 show_default=True,
              help="Max MusicBrainz lookups per day.")
@click.option("--claude-limit",   default=100,                 show_default=True,
              help="Max Claude AI lookups per day.")
@click.option("--no-browser",     is_flag=True,
              help="Don't auto-open browser tab.")
@click.option("--run-now",        is_flag=True,
              help="Trigger enrichment batch immediately on startup.")
@click.option("--poll-watcher",   is_flag=True,
              help="Use polling observer (required for NAS/network mounts).")
@click.option("--watch-interval", default=60,                  show_default=True,
              help="Poll interval in seconds (polling mode only).")
def main(port: int, db: str, daily_limit: int, claude_limit: int,
         no_browser: bool, run_now: bool,
         poll_watcher: bool, watch_interval: int) -> None:
    """Organise a music library using ID3 / audio metadata tags.

    Scans /input, moves matched tracks to /destination,
    and enriches unmatched tracks via heuristics / MusicBrainz / Claude.
    """
    global _enrichment_scheduler
    _config["db_path"] = db

    # ── Banner ────────────────────────────────────────────────────────────────
    console.print()
    console.print(
        Panel.fit(
            Align.center(
                Text.assemble(
                    ("  ID3 Music Organiser  ", "bold white on #4c1d95"),
                    ("  v" + VERSION, "bold #a78bfa on #4c1d95"),
                )
            ),
            border_style="#7c3aed",
            padding=(0, 2),
        )
    )
    console.print()

    info = Table(box=None, show_header=False, padding=(0, 2))
    info.add_column(justify="right", style="dim")
    info.add_column(style="cyan")
    info.add_row("Source",      SOURCE_DIR)
    info.add_row("Destination", PROCESSED_BASE)
    info.add_row("Database",    db)
    console.print(info)
    console.print()
    console.rule(style="dim #7c3aed")
    console.print()

    # ── Init DB ───────────────────────────────────────────────────────────────
    _init_db()
    _migrate_db()

    # ── Phase 1: collect files ────────────────────────────────────────────────
    files = _phase1_collect_files()
    console.print()

    # ── Phase 2 & 3: extract + summarise ─────────────────────────────────────
    if files:
        total, matched = _phase2_extract_and_store(files)
        _phase3_summary(total, matched)
    else:
        console.print("[yellow]  No music files found in /input.[/yellow]")
    console.print()
    console.rule(style="dim #7c3aed")
    console.print()

    # ── Enrichment scheduler ──────────────────────────────────────────────────
    cfg = {
        "source_dir":     SOURCE_DIR,
        "processed_base": PROCESSED_BASE,
        "unmatched_base": UNMATCHED_BASE,
    }
    _enrichment_scheduler = EnrichmentScheduler(db, cfg, daily_limit, claude_limit)
    _enrichment_scheduler.start()
    console.print(
        "[dim]Enrichment scheduler started "
        "(Stage 1 running now · Stage 2/3 at 02:00 daily).[/dim]"
    )

    if run_now:
        console.print("[bold cyan]Triggering immediate enrichment batch…[/bold cyan]")
        _enrichment_scheduler.run_now()

    # ── Directory watcher ─────────────────────────────────────────────────────
    _start_watcher(poll_watcher, watch_interval)

    # ── Web server ────────────────────────────────────────────────────────────
    url = f"http://localhost:{port}"
    console.print(
        Panel.fit(
            Text.assemble(
                ("Web interface ready\n\n", "bold white"),
                ("  →  ", "dim"),
                (url, "bold cyan underline"),
                ("\n\n", ""),
                ("  Review destinations, trigger moves.\n", "dim"),
                ("  Press Ctrl+C to stop the server.", "dim"),
            ),
            border_style="green",
            padding=(1, 2),
        )
    )
    console.print()

    if not no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
