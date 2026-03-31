import csv
import io
import sqlite3
import pytest

import id3_organiser as app_module
from id3_organiser import app


@pytest.fixture
def client(tmp_path):
    db = str(tmp_path / "test.db")
    app_module._config["db_path"] = db
    app_module._config["source_dir"] = str(tmp_path)
    app_module._config["processed_base"] = str(tmp_path / "processed")
    app_module._config["unmatched_base"] = str(tmp_path / "unmatched")
    app_module._init_db()

    # Insert two test tracks
    conn = sqlite3.connect(db)
    conn.execute("""
        INSERT INTO tracks
            (original_path, filename, artist, album, track_number,
             title, genre, year, duration, destination_path, matched, status, scanned_at)
        VALUES
            ('/music/track1.mp3', 'track1.mp3', 'Artist A', 'Album X', '01',
             'Song One', 'Rock', '2020', 210.5,
             '/processed/Artist A/Album X/01. Song One.mp3', 1, 'pending', '2026-01-01'),
            ('/music/track2.mp3', 'track2.mp3', NULL, NULL, NULL,
             NULL, NULL, NULL, NULL,
             '/unmatched/track2.mp3', 0, 'pending', '2026-01-01')
    """)
    conn.commit()
    conn.close()

    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_csv_export_status_200(client):
    r = client.get("/api/export/csv")
    assert r.status_code == 200


def test_csv_export_content_type(client):
    r = client.get("/api/export/csv")
    assert r.content_type.startswith("text/csv")


def test_csv_export_content_disposition(client):
    r = client.get("/api/export/csv")
    assert "attachment" in r.headers["Content-Disposition"]
    assert "music_library.csv" in r.headers["Content-Disposition"]


def test_csv_export_has_header_row(client):
    r = client.get("/api/export/csv")
    reader = csv.reader(io.StringIO(r.data.decode("utf-8")))
    header = next(reader)
    assert header == [
        "Filename", "Artist", "Album", "Track Number",
        "Title", "Genre", "Year", "Duration",
        "Original Path", "Destination Path",
    ]


def test_csv_export_row_count(client):
    r = client.get("/api/export/csv")
    reader = csv.reader(io.StringIO(r.data.decode("utf-8")))
    rows = list(reader)
    # 1 header + 2 data rows
    assert len(rows) == 3


def test_csv_export_row_values(client):
    r = client.get("/api/export/csv")
    reader = csv.reader(io.StringIO(r.data.decode("utf-8")))
    next(reader)  # skip header
    first = next(reader)
    assert first[0] == "track1.mp3"
    assert first[1] == "Artist A"
    assert first[2] == "Album X"
    assert first[3] == "01"
    assert first[4] == "Song One"
    assert first[5] == "Rock"
    assert first[6] == "2020"
    assert first[8] == "/music/track1.mp3"
    assert first[9] == "/processed/Artist A/Album X/01. Song One.mp3"


def test_csv_export_null_fields_become_empty_string(client):
    r = client.get("/api/export/csv")
    reader = csv.reader(io.StringIO(r.data.decode("utf-8")))
    next(reader)  # skip header
    next(reader)  # skip first row
    second = next(reader)
    assert second[1] == ""   # artist is NULL
    assert second[4] == ""   # title is NULL
