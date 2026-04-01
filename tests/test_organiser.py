import pytest
from pathlib import Path
import id3_organiser as mod


def test_calculate_destination_matched():
    meta = {"artist": "Radiohead", "album": "OK Computer", "title": "Karma Police", "track_number": "3"}
    dest, matched = mod._calculate_destination(meta, f"{mod.SOURCE_DIR}/some/track.mp3")
    assert matched is True
    expected = str(Path(mod.PROCESSED_BASE) / "Radiohead" / "OK Computer" / "03. Karma Police.mp3")
    assert dest == expected


def test_calculate_destination_unmatched_missing_title():
    meta = {"artist": "Radiohead", "album": "OK Computer"}
    dest, matched = mod._calculate_destination(meta, f"{mod.SOURCE_DIR}/some/track.mp3")
    assert matched is False
    assert dest.startswith(str(Path(mod.UNMATCHED_BASE)))


def test_calculate_destination_unmatched_all_missing():
    dest, matched = mod._calculate_destination({}, "/some/other/track.flac")
    assert matched is False
    assert "track.flac" in dest


import sqlite3
import unittest.mock


@pytest.fixture
def db_path(tmp_path):
    db = str(tmp_path / "test.db")
    mod._config["db_path"] = db
    mod._init_db()
    mod._migrate_db()
    return db


def test_ingest_file_inserts_row(db_path, tmp_path):
    fpath = str(tmp_path / "song.mp3")
    Path(fpath).touch()
    with unittest.mock.patch.object(mod, "_extract_metadata", return_value={
        "artist": "Test Artist", "album": "Test Album", "title": "Test Song",
        "track_number": "1", "genre": None, "year": None, "duration": 180.0,
    }):
        mod._ingest_file(fpath)
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT * FROM tracks WHERE original_path=?", (fpath,)).fetchone()
    conn.close()
    assert row is not None


def test_ingest_file_upserts_on_rescan(db_path, tmp_path):
    fpath = str(tmp_path / "song.mp3")
    Path(fpath).touch()
    tags = {"artist": "A", "album": "B", "title": "C", "track_number": None,
            "genre": None, "year": None, "duration": None}
    with unittest.mock.patch.object(mod, "_extract_metadata", return_value=tags):
        mod._ingest_file(fpath)
        mod._ingest_file(fpath)
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM tracks WHERE original_path=?", (fpath,)).fetchone()[0]
    conn.close()
    assert count == 1


def test_music_file_handler_on_created(db_path, tmp_path):
    fpath = str(tmp_path / "new_track.mp3")
    Path(fpath).touch()
    with unittest.mock.patch.object(mod, "_ingest_file") as mock_ingest:
        handler = mod.MusicFileHandler()
        event = unittest.mock.Mock()
        event.is_directory = False
        event.src_path = fpath
        handler.on_created(event)
        mock_ingest.assert_called_once_with(fpath)


def test_music_file_handler_ignores_directories(db_path, tmp_path):
    with unittest.mock.patch.object(mod, "_ingest_file") as mock_ingest:
        handler = mod.MusicFileHandler()
        event = unittest.mock.Mock()
        event.is_directory = True
        event.src_path = str(tmp_path)
        handler.on_created(event)
        mock_ingest.assert_not_called()


def test_music_file_handler_ignores_non_audio(db_path, tmp_path):
    fpath = str(tmp_path / "readme.txt")
    with unittest.mock.patch.object(mod, "_ingest_file") as mock_ingest:
        handler = mod.MusicFileHandler()
        event = unittest.mock.Mock()
        event.is_directory = False
        event.src_path = fpath
        handler.on_created(event)
        mock_ingest.assert_not_called()


@pytest.mark.parametrize("artist,album,title", [
    ("Unknown Artist", "Real Album",    "Real Title"),
    ("Real Artist",    "Unknown Album", "Real Title"),
    ("Real Artist",    "Real Album",    "Unknown Track"),
    ("UNKNOWN ARTIST", "Real Album",    "Real Title"),   # case variants
    ("Real Artist",    "UNKNOWN ALBUM", "Real Title"),
    ("Real Artist",    "Real Album",    "unknown track"),
])
def test_extract_metadata_nulls_placeholder_tags(artist, album, title):
    """Any 'Unknown X' placeholder must come back as None so the track is flagged for enrichment."""
    import unittest.mock
    fake_audio = unittest.mock.MagicMock()
    fake_audio.get.side_effect = lambda key: (
        [artist]  if key == "artist" else
        [album]   if key == "album"  else
        [title]   if key == "title"  else
        None
    )
    fake_audio.info.length = 120.0
    with unittest.mock.patch("id3_organiser.MutagenFile", return_value=fake_audio):
        meta = mod._extract_metadata("fake.mp3")
    if artist.lower() == "unknown artist":
        assert meta["artist"] is None, f"expected artist=None, got {meta['artist']!r}"
    if album.lower() == "unknown album":
        assert meta["album"] is None,  f"expected album=None, got {meta['album']!r}"
    if title.lower() == "unknown track":
        assert meta["title"] is None,  f"expected title=None, got {meta['title']!r}"
