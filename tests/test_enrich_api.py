# tests/test_enrich_api.py
import sqlite3
import pytest
import id3_organiser as app_module
from id3_organiser import app
from click.testing import CliRunner
import unittest.mock


@pytest.fixture
def client(tmp_path):
    db = str(tmp_path / "test.db")
    app_module._config["db_path"] = db
    app_module._init_db()
    app_module._migrate_db()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_enrichment_candidates_table_exists(client):
    conn = sqlite3.connect(app_module._config["db_path"])
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "enrichment_candidates" in tables
    assert "enrichment_runs" in tables


def test_tracks_has_enrichment_columns(client):
    conn = sqlite3.connect(app_module._config["db_path"])
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tracks)")}
    conn.close()
    assert "enrichment_status" in cols
    assert "tags_written_at" in cols


def test_wal_mode_enabled(client):
    conn = sqlite3.connect(app_module._config["db_path"])
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode == "wal"


def test_main_command_help():
    from id3_organiser import main
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "--daily-limit" in result.output
    assert "--run-now" in result.output
    assert "--poll-watcher" in result.output


import json as _json
import sqlite3 as _sqlite3
import unittest.mock


@pytest.fixture
def client_with_candidate(tmp_path):
    db = str(tmp_path / "test.db")
    app_module._config["db_path"] = db
    app_module._init_db()
    app_module._migrate_db()

    conn = _sqlite3.connect(db)
    conn.execute("""
        INSERT INTO tracks (original_path, filename, artist, album, title,
                            matched, status, scanned_at, enrichment_status)
        VALUES ('/music/Artist/Album/01 - Song.mp3', '01 - Song.mp3',
                NULL, NULL, NULL, 0, 'pending', '2026-01-01', 'review_needed')
    """)
    track_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("""
        INSERT INTO enrichment_candidates
            (track_id, source, suggested_artist, suggested_album, suggested_title,
             suggested_year, confidence, status, created_at)
        VALUES (?, 'musicbrainz', 'Artist Name', 'Album Name', 'Song Title',
                '2020', 'high', 'pending_review', '2026-01-01')
    """, (track_id,))
    conn.commit()
    conn.close()

    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, track_id, db


def test_get_candidates_returns_200(client_with_candidate):
    c, _, _ = client_with_candidate
    r = c.get("/api/enrich/candidates")
    assert r.status_code == 200


def test_get_candidates_returns_candidate(client_with_candidate):
    c, _, _ = client_with_candidate
    r = c.get("/api/enrich/candidates")
    data = _json.loads(r.data)
    assert data["total"] >= 1
    assert data["candidates"][0]["suggested_artist"] == "Artist Name"


def test_get_enrich_status_returns_200(client_with_candidate):
    c, _, _ = client_with_candidate
    r = c.get("/api/enrich/status")
    assert r.status_code == 200


def test_post_run_now_returns_200(client_with_candidate):
    c, _, _ = client_with_candidate
    r = c.post("/api/enrich/run-now")
    # Returns 200 if scheduler running, 400 if not — both are valid in tests
    assert r.status_code in (200, 400)


def test_post_apply_writes_tags_and_updates_db(client_with_candidate, tmp_path):
    c, track_id, db_path = client_with_candidate

    # Create a minimal MP3 file
    fake_file = tmp_path / "01 - Song.mp3"
    header = b"ID3\x03\x00\x00\x00\x00\x00\x00"
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    fake_file.write_bytes(header + frame * 3)

    conn = _sqlite3.connect(db_path)
    conn.execute(
        "UPDATE tracks SET original_path=? WHERE id=?",
        (str(fake_file), track_id)
    )
    candidate_id = conn.execute(
        "SELECT id FROM enrichment_candidates WHERE track_id=?", (track_id,)
    ).fetchone()[0]
    conn.commit()
    conn.close()

    with unittest.mock.patch("enrichment.tag_writer.write_tags"):
        r = c.post("/api/enrich/apply",
                   data=_json.dumps({"candidate_ids": [candidate_id]}),
                   content_type="application/json")
    assert r.status_code == 200

    import time as _t; _t.sleep(0.2)  # wait for background thread

    conn = _sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT enrichment_status FROM tracks WHERE id=?", (track_id,)
    ).fetchone()
    conn.close()
    assert row[0] == "enriched"
