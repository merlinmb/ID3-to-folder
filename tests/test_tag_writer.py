# tests/test_tag_writer.py
import shutil
import pytest
from pathlib import Path
from mutagen import File as MutagenFile
from enrichment.tag_writer import write_tags, TagWriteError


@pytest.fixture
def sample_mp3(tmp_path):
    """Create a minimal valid MP3 for tag writing tests."""
    dst = tmp_path / "track.mp3"
    # Create a minimal valid MP3 file with ID3v2 and MPEG frames
    # ID3v2.3 header (10 bytes)
    id3_header = b"ID3\x03\x00\x00\x00\x00\x00\x00"

    # Multiple MPEG frame headers to ensure mutagen finds valid sync
    # MPEG frame header: FF FB (sync + MPEG1 Layer3 44.1kHz no CRC)
    # followed by minimal frame data (417 bytes per frame for this config)
    mpeg_frames = b"\xff\xfb\x90\x00" + (b"\x00" * 413)
    mpeg_frames *= 3  # Add 3 frames to be safe

    dst.write_bytes(id3_header + mpeg_frames)
    return str(dst)


def test_write_tags_sets_artist(sample_mp3):
    write_tags(sample_mp3, {"artist": "Test Artist", "album": "Test Album",
                             "title": "Test Title", "year": "2020",
                             "track_number": "01"})
    audio = MutagenFile(sample_mp3, easy=True)
    assert audio["artist"][0] == "Test Artist"


def test_write_tags_sets_album(sample_mp3):
    write_tags(sample_mp3, {"artist": "A", "album": "My Album",
                             "title": "T", "year": "2021", "track_number": None})
    audio = MutagenFile(sample_mp3, easy=True)
    assert audio["album"][0] == "My Album"


def test_write_tags_sets_title(sample_mp3):
    write_tags(sample_mp3, {"artist": "A", "album": "B",
                             "title": "My Song", "year": "2022", "track_number": "03"})
    audio = MutagenFile(sample_mp3, easy=True)
    assert audio["title"][0] == "My Song"


def test_write_tags_skips_none_values(sample_mp3):
    write_tags(sample_mp3, {"artist": "A", "album": None,
                             "title": "T", "year": None, "track_number": None})
    audio = MutagenFile(sample_mp3, easy=True)
    assert "album" not in audio or audio.get("album") is None


def test_write_tags_raises_on_missing_file():
    with pytest.raises(TagWriteError):
        write_tags("/nonexistent/path/track.mp3", {"artist": "A"})
