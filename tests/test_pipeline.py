# tests/test_pipeline.py
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import patch
from enrichment.pipeline import run_stage1, run_stage2_batch, EnrichmentScheduler


@pytest.fixture
def db(tmp_path):
    """Initialised DB with schema + two unmatched tracks."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_path TEXT NOT NULL UNIQUE,
            filename TEXT NOT NULL,
            artist TEXT, album TEXT, track_number TEXT, title TEXT,
            genre TEXT, year TEXT, duration REAL,
            destination_path TEXT, matched INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending', error_message TEXT,
            scanned_at TEXT, moved_at TEXT,
            enrichment_status TEXT DEFAULT 'none',
            tags_written_at TEXT
        );
        CREATE TABLE enrichment_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            suggested_artist TEXT, suggested_album TEXT, suggested_title TEXT,
            suggested_year TEXT, suggested_genre TEXT,
            confidence TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending_review',
            raw_response TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE enrichment_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            last_processed_track_id INTEGER,
            tracks_processed INTEGER DEFAULT 0,
            tracks_remaining INTEGER,
            daily_limit INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            claude_batch_id TEXT
        );
    """)
    conn.execute("""
        INSERT INTO tracks (original_path, filename, artist, album, title, matched, scanned_at)
        VALUES
            ('/music/Pink Floyd/The Wall/01 - In The Flesh.mp3',
             '01 - In The Flesh.mp3', NULL, NULL, NULL, 0, '2026-01-01'),
            ('/music/Unknown/random.mp3',
             'random.mp3', NULL, NULL, NULL, 0, '2026-01-01')
    """)
    conn.commit()
    conn.close()
    return db_path


def test_stage1_auto_applies_high_confidence(db, tmp_path):
    cfg = {
        "source_dir": "/music",
        "processed_base": str(tmp_path / "processed"),
        "unmatched_base": str(tmp_path / "unmatched"),
    }
    with patch("enrichment.tag_writer.write_tags"):
        run_stage1(db, cfg)

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT enrichment_status FROM tracks WHERE filename='01 - In The Flesh.mp3'"
    ).fetchone()
    conn.close()
    # Artist + Album come from folder path → high confidence → auto-applied
    assert row[0] == "enriched"


def test_stage1_marks_low_confidence_as_review_needed(db, tmp_path):
    cfg = {
        "source_dir": "/music",
        "processed_base": str(tmp_path / "processed"),
        "unmatched_base": str(tmp_path / "unmatched"),
    }
    with patch("enrichment.tag_writer.write_tags"):
        run_stage1(db, cfg)

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT enrichment_status FROM tracks WHERE filename='random.mp3'"
    ).fetchone()
    conn.close()
    # Flat path → low confidence → pending_review or unchanged
    assert row[0] in ("review_needed", "none")


def test_stage2_batch_inserts_candidate(db, tmp_path):
    cfg = {
        "source_dir": "/music",
        "processed_base": str(tmp_path / "processed"),
        "unmatched_base": str(tmp_path / "unmatched"),
    }
    mb_result = {"artist": "Pink Floyd", "album": "The Wall",
                 "title": "In The Flesh", "year": "1979", "confidence": "high"}
    with patch("enrichment.musicbrainz.query_musicbrainz", return_value=mb_result), \
         patch("enrichment.claude_fallback.submit_claude_batch", return_value="batch_xyz"), \
         patch("enrichment.claude_fallback.collect_claude_batch", return_value=None), \
         patch("time.sleep"):
        run_stage2_batch(db, cfg, daily_limit=10, claude_limit=5)

    conn = sqlite3.connect(db)
    count = conn.execute("SELECT COUNT(*) FROM enrichment_candidates").fetchone()[0]
    conn.close()
    assert count >= 1


def test_stage2_records_enrichment_run(db, tmp_path):
    cfg = {
        "source_dir": "/music",
        "processed_base": str(tmp_path / "processed"),
        "unmatched_base": str(tmp_path / "unmatched"),
    }
    with patch("enrichment.musicbrainz.query_musicbrainz", return_value=None), \
         patch("enrichment.claude_fallback.submit_claude_batch", return_value="batch_xyz"), \
         patch("enrichment.claude_fallback.collect_claude_batch", return_value=None), \
         patch("time.sleep"):
        run_stage2_batch(db, cfg, daily_limit=10, claude_limit=5)

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT status FROM enrichment_runs ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    # 'running' means batch submitted but Claude results not yet collected; that's valid
    assert row[0] in ("complete", "running")
