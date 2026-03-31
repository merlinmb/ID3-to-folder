# enrichment/pipeline.py
import json
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import schedule

from .heuristics import apply_folder_grouping, extract_from_path
from .tag_writer import TagWriteError, write_tags

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize(name: str | None, fallback: str = "Unknown") -> str:
    if not name:
        return fallback
    name = _UNSAFE.sub("_", name).strip(". ")
    return name or fallback


def _parse_track_number(raw: str | None) -> str | None:
    if not raw:
        return None
    m = re.match(r"(\d+)", str(raw))
    return f"{int(m.group(1)):02d}" if m else None


def _calc_destination(meta: dict, original_path: str, cfg: dict) -> tuple[str, bool]:
    artist = meta.get("artist")
    album  = meta.get("album")
    title  = meta.get("title")
    ext    = Path(original_path).suffix.lower()

    if artist and album and title:
        tn     = _parse_track_number(meta.get("track_number"))
        prefix = f"{tn}. " if tn else ""
        fname  = _sanitize(f"{prefix}{title}") + ext
        dest   = Path(cfg["processed_base"]) / _sanitize(artist) / _sanitize(album) / fname
        return str(dest), True
    else:
        try:
            rel = Path(original_path).relative_to(cfg["source_dir"])
        except ValueError:
            rel = Path(Path(original_path).name)
        return str(Path(cfg["unmatched_base"]) / rel), False


def _get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def run_stage1(db_path: str, cfg: dict) -> None:
    """Heuristic enrichment over all unmatched tracks with enrichment_status='none'."""
    conn = _get_conn(db_path)
    rows = conn.execute(
        "SELECT id, original_path, filename, artist, album, title, track_number "
        "FROM tracks WHERE matched=0 AND enrichment_status='none'"
    ).fetchall()
    tracks = [dict(r) for r in rows]
    conn.close()

    tracks = apply_folder_grouping(tracks)
    now = datetime.now().isoformat()

    for track in tracks:
        suggestion = extract_from_path(track["original_path"], cfg["source_dir"])
        merged = {
            "artist":       track["artist"] or suggestion.get("artist"),
            "album":        track["album"]  or suggestion.get("album"),
            "title":        track["title"]  or suggestion.get("title"),
            "track_number": track["track_number"] or suggestion.get("track_number"),
        }
        confidence = suggestion.get("confidence", "low")

        if (confidence == "high"
                and merged["artist"] and merged["album"] and merged["title"]):
            try:
                write_tags(track["original_path"], merged)
            except TagWriteError:
                pass
            dest, is_matched = _calc_destination(merged, track["original_path"], cfg)
            conn = _get_conn(db_path)
            conn.execute(
                "UPDATE tracks SET artist=?, album=?, title=?, track_number=?, "
                "destination_path=?, matched=?, enrichment_status='enriched', "
                "tags_written_at=? WHERE id=?",
                (merged["artist"], merged["album"], merged["title"],
                 merged["track_number"], dest, 1 if is_matched else 0, now, track["id"])
            )
            conn.commit()
            conn.close()
        else:
            if merged.get("artist") or merged.get("album") or merged.get("title"):
                conn = _get_conn(db_path)
                conn.execute(
                    "INSERT OR IGNORE INTO enrichment_candidates "
                    "(track_id, source, suggested_artist, suggested_album, "
                    "suggested_title, confidence, status, created_at) "
                    "VALUES (?, 'heuristic', ?, ?, ?, ?, 'pending_review', ?)",
                    (track["id"], merged.get("artist"), merged.get("album"),
                     merged.get("title"), confidence, now)
                )
                conn.execute(
                    "UPDATE tracks SET enrichment_status='review_needed' WHERE id=?",
                    (track["id"],)
                )
                conn.commit()
                conn.close()


def run_stage2_batch(db_path: str, cfg: dict, daily_limit: int, claude_limit: int) -> None:
    """MusicBrainz for unmatched tracks, then submit/collect Claude batch."""
    from .musicbrainz import query_musicbrainz
    from .claude_fallback import submit_claude_batch, collect_claude_batch

    now = datetime.now().isoformat()

    # --- Try to collect a pending Claude batch from a previous run ---
    conn = _get_conn(db_path)
    pending_run = conn.execute(
        "SELECT id, claude_batch_id FROM enrichment_runs "
        "WHERE claude_batch_id IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    if pending_run:
        results = collect_claude_batch(pending_run["claude_batch_id"])
        if results is not None:
            for item in results:
                s = item["suggestion"]
                conn = _get_conn(db_path)
                conn.execute(
                    "INSERT INTO enrichment_candidates "
                    "(track_id, source, suggested_artist, suggested_album, "
                    "suggested_title, suggested_year, confidence, status, created_at) "
                    "VALUES (?, 'claude', ?, ?, ?, ?, 'low', 'pending_review', ?)",
                    (item["track_id"], s.get("artist"), s.get("album"),
                     s.get("title"), s.get("year"), now)
                )
                conn.execute(
                    "UPDATE tracks SET enrichment_status='review_needed' WHERE id=?",
                    (item["track_id"],)
                )
                conn.commit()
                conn.close()
            # Clear the batch_id now that it's collected
            conn = _get_conn(db_path)
            conn.execute(
                "UPDATE enrichment_runs SET claude_batch_id=NULL WHERE id=?",
                (pending_run["id"],)
            )
            conn.commit()
            conn.close()

    # --- Insert a new run record ---
    conn = _get_conn(db_path)
    conn.execute(
        "INSERT INTO enrichment_runs (started_at, daily_limit, status) VALUES (?, ?, 'running')",
        (now, daily_limit)
    )
    conn.commit()
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # --- Tracks still needing enrichment ---
    rows = conn.execute(
        "SELECT t.id, t.original_path, t.filename, t.artist, t.album, t.title "
        "FROM tracks t "
        "WHERE t.matched=0 AND t.enrichment_status NOT IN ('enriched') "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM enrichment_candidates ec "
        "  WHERE ec.track_id=t.id AND ec.source='musicbrainz'"
        ") "
        "LIMIT ?",
        (daily_limit,)
    ).fetchall()
    tracks = [dict(r) for r in rows]
    conn.close()

    mb_processed = 0
    needs_claude: list[dict] = []

    for track in tracks:
        result = query_musicbrainz(
            track.get("artist"), track.get("album"),
            track.get("title") or Path(track["original_path"]).stem
        )
        mb_processed += 1
        time.sleep(1.1)  # MusicBrainz: 1 req/sec

        if result:
            conn = _get_conn(db_path)
            conn.execute(
                "INSERT INTO enrichment_candidates "
                "(track_id, source, suggested_artist, suggested_album, "
                "suggested_title, suggested_year, confidence, raw_response, "
                "status, created_at) VALUES (?, 'musicbrainz', ?, ?, ?, ?, ?, ?, 'pending_review', ?)",
                (track["id"], result.get("artist"), result.get("album"),
                 result.get("title"), result.get("year"), result.get("confidence", "low"),
                 json.dumps(result), now)
            )
            conn.execute(
                "UPDATE tracks SET enrichment_status='review_needed' WHERE id=?",
                (track["id"],)
            )
            conn.commit()
            conn.close()
        else:
            needs_claude.append(track)

    # --- Submit Claude batch for tracks MusicBrainz couldn't resolve ---
    batch_id = None
    if needs_claude and claude_limit > 0:
        folder_map: dict[str, list[str]] = {}
        for t in needs_claude:
            folder = str(Path(t["original_path"]).parent)
            if folder not in folder_map:
                folder_map[folder] = []
            folder_map[folder].append(t["filename"])

        batch_inputs = []
        for t in needs_claude[:claude_limit]:
            folder = str(Path(t["original_path"]).parent)
            neighbours = [f for f in folder_map.get(folder, []) if f != t["filename"]]
            batch_inputs.append({
                "track_id":    t["id"],
                "filename":    t["filename"],
                "folder_path": folder,
                "partial_tags": {k: t.get(k) for k in ("artist", "album", "title")},
                "neighbours":  neighbours[:10],
            })

        try:
            batch_id = submit_claude_batch(batch_inputs)
        except Exception:
            batch_id = None

    # --- Update run record ---
    conn = _get_conn(db_path)
    conn.execute(
        "UPDATE enrichment_runs SET tracks_processed=?, status=?, claude_batch_id=? WHERE id=?",
        (mb_processed, "running" if batch_id else "complete", batch_id, run_id)
    )
    conn.commit()
    conn.close()


class EnrichmentScheduler:
    """Long-lived scheduler: Stage 1 on startup, Stage 2/3 daily."""

    def __init__(self, db_path: str, cfg: dict, daily_limit: int, claude_limit: int):
        self.db_path = db_path
        self.cfg = cfg
        self.daily_limit = daily_limit
        self.claude_limit = claude_limit
        self._run_now_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._status: dict = {
            "running": False,
            "tracks_processed_today": 0,
            "tracks_remaining": 0,
            "next_run": "02:00 daily",
            "last_run": None,
        }

    def get_status(self) -> dict:
        self._refresh_remaining()
        with self._lock:
            return dict(self._status)

    def run_now(self) -> None:
        self._run_now_event.set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _refresh_remaining(self) -> None:
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE matched=0 "
                "AND enrichment_status NOT IN ('enriched')"
            ).fetchone()
            conn.close()
            with self._lock:
                self._status["tracks_remaining"] = row[0] if row else 0
        except Exception:
            pass

    def _run_batch(self) -> None:
        with self._lock:
            self._status["running"] = True
            self._status["last_run"] = datetime.now().isoformat()
        try:
            run_stage2_batch(self.db_path, self.cfg, self.daily_limit, self.claude_limit)
            with self._lock:
                self._status["tracks_processed_today"] = self.daily_limit
        finally:
            with self._lock:
                self._status["running"] = False
            self._refresh_remaining()

    def _loop(self) -> None:
        run_stage1(self.db_path, self.cfg)
        schedule.every().day.at("02:00").do(self._run_batch)
        while True:
            schedule.run_pending()
            if self._run_now_event.wait(timeout=10):
                self._run_now_event.clear()
                self._run_batch()
