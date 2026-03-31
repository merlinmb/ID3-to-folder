# tests/test_musicbrainz.py
from unittest.mock import patch, MagicMock
import pytest
from enrichment.musicbrainz import query_musicbrainz, MusicBrainzError


def _mock_mb_result(title="Wish You Were Here", artist="Pink Floyd",
                    album="Wish You Were Here", date="1975"):
    return {
        "recording-list": [{
            "title": title,
            "artist-credit-phrase": artist,
            "release-list": [{"title": album, "date": date}],
        }]
    }


def test_high_confidence_on_exact_match():
    with patch("musicbrainzngs.search_recordings", return_value=_mock_mb_result()):
        result = query_musicbrainz("Pink Floyd", "Wish You Were Here", "Wish You Were Here")
    assert result["confidence"] == "high"
    assert result["artist"] == "Pink Floyd"
    assert result["title"] == "Wish You Were Here"
    assert result["year"] == "1975"


def test_low_confidence_on_fuzzy_match():
    with patch("musicbrainzngs.search_recordings",
               return_value=_mock_mb_result(title="Something Completely Different")):
        result = query_musicbrainz("Pink Floyd", None, "Wish You Were Here")
    assert result["confidence"] == "low"


def test_returns_none_when_no_results():
    with patch("musicbrainzngs.search_recordings",
               return_value={"recording-list": []}):
        result = query_musicbrainz("Unknown", None, "Unknown Track")
    assert result is None


def test_returns_none_on_api_error():
    import musicbrainzngs
    with patch("musicbrainzngs.search_recordings",
               side_effect=musicbrainzngs.ResponseError(cause=Exception("500"))):
        result = query_musicbrainz("Artist", "Album", "Title")
    assert result is None


def test_year_extracted_from_date_prefix():
    mock = _mock_mb_result(date="1973-03-01")
    with patch("musicbrainzngs.search_recordings", return_value=mock):
        result = query_musicbrainz("Pink Floyd", "Dark Side", "Money")
    assert result["year"] == "1973"
