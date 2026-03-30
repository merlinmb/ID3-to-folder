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

console = Console()
app = Flask(__name__)

# Populated at CLI startup; read by Flask routes
_config: dict = {}
# Per-move SSE event queue (reset each time a move is triggered)
_move_event_queue: queue.Queue = queue.Queue()

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
    moved_at         TEXT
);
"""


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_config["db_path"])
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _get_db() as conn:
        conn.executescript(_SCHEMA)


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

    meta = {
        "title":        tag("title"),
        "artist":       tag("artist") or tag("albumartist"),
        "album":        tag("album"),
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
    matched  → /processed/<Artist>/<Album>/<NN>. <Title>.ext
    unmatched → /unmatched/<relative-original-path>
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
            Path(_config["processed_base"])
            / _sanitize(artist)
            / _sanitize(album)
            / filename
        )
        return str(dest), True
    else:
        try:
            rel = Path(original_path).relative_to(_config["source_dir"])
        except ValueError:
            rel = Path(Path(original_path).name)
        return str(Path(_config["unmatched_base"]) / rel), False


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
        for root, _dirs, files in os.walk(_config["source_dir"]):
            for fname in files:
                if Path(fname).suffix.lower() in SUPPORTED_EXTENSIONS:
                    found.append(os.path.join(root, fname))

    console.print(
        f"  [bold green]✓[/bold green]  Found "
        f"[bold white]{len(found)}[/bold white] music files"
        f"  [dim]({_config['source_dir']})[/dim]"
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
        f"[dim]→  {_config['processed_base']}[/dim]",
    )
    tbl.add_row(
        "Unmatched",
        f"[bold yellow]{unmatched}[/bold yellow]  "
        f"[dim]→  {_config['unmatched_base']}[/dim]",
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
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("source_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--db",        default="music_organiser.db", show_default=True,
              help="SQLite database path.")
@click.option("--processed", default="./processed",         show_default=True,
              help="Base directory for organised files.")
@click.option("--unmatched", default="./unmatched",         show_default=True,
              help="Base directory for unmatched files.")
@click.option("--port",      default=5000,                  show_default=True,
              help="Web server port.")
@click.option("--no-browser", is_flag=True,
              help="Don't auto-open browser tab.")
def main(source_dir: str, db: str, processed: str, unmatched: str,
         port: int, no_browser: bool) -> None:
    """Organise a music library using ID3 / audio metadata tags.

    SOURCE_DIR  Root directory to scan for .mp3 / .flac / .aac / .m4a files.
    """
    _config["db_path"]        = db
    _config["source_dir"]     = os.path.abspath(source_dir)
    _config["processed_base"] = os.path.abspath(processed)
    _config["unmatched_base"] = os.path.abspath(unmatched)

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
    info.add_row("Source",     _config["source_dir"])
    info.add_row("Database",   db)
    info.add_row("Processed",  _config["processed_base"])
    info.add_row("Unmatched",  _config["unmatched_base"])
    console.print(info)
    console.print()
    console.rule(style="dim #7c3aed")
    console.print()

    # ── Init DB ───────────────────────────────────────────────────────────────
    _init_db()

    # ── Phase 1: collect files ────────────────────────────────────────────────
    files = _phase1_collect_files()
    console.print()

    if not files:
        console.print("[yellow]  No music files found — nothing to do.[/yellow]")
        return

    # ── Phase 2: extract metadata + persist ───────────────────────────────────
    total, matched = _phase2_extract_and_store(files)

    # ── Phase 3: summary ──────────────────────────────────────────────────────
    _phase3_summary(total, matched)
    console.print()
    console.rule(style="dim #7c3aed")
    console.print()

    # ── Web server ────────────────────────────────────────────────────────────
    url = f"http://localhost:{port}"
    console.print(
        Panel.fit(
            Text.assemble(
                ("Web interface ready\n\n", "bold white"),
                ("  →  ", "dim"),
                (url, "bold cyan underline"),
                ("\n\n", ""),
                ("  Review and edit destinations, then trigger file moves.\n", "dim"),
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
