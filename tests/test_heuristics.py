# tests/test_heuristics.py
import pytest
from enrichment.heuristics import extract_from_path, apply_folder_grouping


def test_extract_artist_album_from_deep_path():
    result = extract_from_path(
        "/music/Pink Floyd/The Wall/01 - In The Flesh.mp3",
        "/music"
    )
    assert result["artist"] == "Pink Floyd"
    assert result["album"] == "The Wall"
    assert result["confidence"] == "high"


def test_extract_title_from_numbered_filename():
    result = extract_from_path(
        "/music/Pink Floyd/The Wall/01 - In The Flesh.mp3",
        "/music"
    )
    assert result["title"] == "In The Flesh"
    assert result["track_number"] == "01"


def test_extract_title_from_dot_numbered_filename():
    result = extract_from_path(
        "/music/Artist/Album/03. Song Title.mp3",
        "/music"
    )
    assert result["title"] == "Song Title"
    assert result["track_number"] == "03"


def test_extract_artist_from_single_folder():
    result = extract_from_path(
        "/music/Radiohead/creep.mp3",
        "/music"
    )
    assert result["artist"] == "Radiohead"
    assert result["confidence"] == "low"


def test_flat_file_returns_low_confidence():
    result = extract_from_path(
        "/music/unknown_track.mp3",
        "/music"
    )
    assert result["confidence"] == "low"
    assert result["artist"] is None


def test_folder_grouping_propagates_artist():
    tracks = [
        {"id": 1, "original_path": "/music/Beatles/t1.mp3", "artist": "The Beatles", "album": None},
        {"id": 2, "original_path": "/music/Beatles/t2.mp3", "artist": "The Beatles", "album": None},
        {"id": 3, "original_path": "/music/Beatles/t3.mp3", "artist": None, "album": None},
        {"id": 4, "original_path": "/music/Beatles/t4.mp3", "artist": None, "album": None},
    ]
    result = apply_folder_grouping(tracks)
    # 2 of 4 = 50%, passes threshold; both null siblings should fill
    null_tracks = [t for t in result if t["id"] in (3, 4)]
    assert all(t["artist"] == "The Beatles" for t in null_tracks)


def test_folder_grouping_does_not_overwrite_existing():
    tracks = [
        {"id": 1, "original_path": "/music/Misc/t1.mp3", "artist": "Artist A", "album": None},
        {"id": 2, "original_path": "/music/Misc/t2.mp3", "artist": "Artist B", "album": None},
        {"id": 3, "original_path": "/music/Misc/t3.mp3", "artist": "Artist A", "album": None},
    ]
    result = apply_folder_grouping(tracks)
    assert result[1]["artist"] == "Artist B"  # not overwritten


def test_folder_grouping_skips_small_folders():
    tracks = [
        {"id": 1, "original_path": "/music/Solo/t1.mp3", "artist": "X", "album": None},
        {"id": 2, "original_path": "/music/Solo/t2.mp3", "artist": None, "album": None},
    ]
    result = apply_folder_grouping(tracks)
    assert result[1]["artist"] is None  # < 3 files, no propagation


def test_folder_grouping_suppresses_minority_artist():
    """Artist present in only 2 of 5 tracks should not propagate."""
    tracks = [
        {"id": 1, "original_path": "/music/Comp/t1.mp3", "artist": "Artist A", "album": None},
        {"id": 2, "original_path": "/music/Comp/t2.mp3", "artist": "Artist A", "album": None},
        {"id": 3, "original_path": "/music/Comp/t3.mp3", "artist": "Artist B", "album": None},
        {"id": 4, "original_path": "/music/Comp/t4.mp3", "artist": None, "album": None},
        {"id": 5, "original_path": "/music/Comp/t5.mp3", "artist": None, "album": None},
    ]
    result = apply_folder_grouping(tracks)
    # "Artist A" has count=2, len=5, threshold=2.5 — should NOT propagate
    assert result[3]["artist"] is None
    assert result[4]["artist"] is None
