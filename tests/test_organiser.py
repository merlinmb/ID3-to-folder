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
