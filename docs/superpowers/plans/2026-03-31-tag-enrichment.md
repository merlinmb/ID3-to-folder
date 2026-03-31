# Tag Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a three-stage enrichment pipeline (heuristics → MusicBrainz → Claude AI) that fills missing ID3 tags for unmatched tracks, writes corrected tags back to audio files, and surfaces a review queue in the web UI.

**Architecture:** A new `enrichment/` Python package handles all pipeline logic; `id3_organiser.py` gains an `enrich` CLI subcommand that runs Stage 1 immediately and starts a long-lived scheduler thread for daily Stage 2/3 batches. The Flask app gains five new API endpoints and `templates/index.html` gains an Enrich tab.

**Tech Stack:** Python 3.11+, mutagen, musicbrainzngs, anthropic SDK (claude-haiku-4-5-20251001), schedule, Flask SSE (existing pattern), SQLite WAL, pytest, Bootstrap 5.3.

---

## File Map

| Action | Path | Responsibility |
|---|---|---|
| Create | `enrichment/__init__.py` | Package exports |
| Create | `enrichment/heuristics.py` | Stage 1: parse paths + filenames, folder grouping |
| Create | `enrichment/tag_writer.py` | Write corrected tags to audio files via mutagen |
| Create | `enrichment/musicbrainz.py` | Stage 2: MusicBrainz API queries + confidence scoring |
| Create | `enrichment/claude_fallback.py` | Stage 3: Claude AI prompt + response parsing |
| Create | `enrichment/pipeline.py` | Orchestrate stages, DB access, daily scheduler thread |
| Modify | `id3_organiser.py` | DB schema, WAL mode, `enrich` CLI command, 5 new Flask routes |
| Modify | `templates/index.html` | Enrich tab: status bar + review queue; enrichment_status filter |
| Modify | `requirements.txt` | Add musicbrainzngs, anthropic, schedule |
| Create | `tests/test_heuristics.py` | Unit tests for Stage 1 |
| Create | `tests/test_tag_writer.py` | Unit tests for tag write-back |
| Create | `tests/test_musicbrainz.py` | Unit tests for MusicBrainz module (mocked) |
| Create | `tests/test_claude_fallback.py` | Unit tests for Claude module (mocked) |
| Create | `tests/test_pipeline.py` | Integration tests for pipeline DB operations |
| Create | `tests/test_enrich_api.py` | Flask route tests for new endpoints |

---

## Task 1: DB Schema — New Tables, New Columns, WAL Mode

**Files:**
- Modify: `id3_organiser.py`
- Test: `tests/test_enrich_api.py` (schema fixture reused in later tasks)

- [ ] **Step 1: Write failing test for new schema**

```python
# tests/test_enrich_api.py
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
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd "e:/GoogleDrive/Arduino/ID3-to-folder"
python -m pytest tests/test_enrich_api.py::test_enrichment_candidates_table_exists -v
```
Expected: FAIL — `_migrate_db` not defined.

- [ ] **Step 3: Update `_SCHEMA` and add `_migrate_db` in `id3_organiser.py`**

Find the existing `_SCHEMA` constant and replace it:

```python
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
    moved_at         TEXT,
    enrichment_status TEXT    DEFAULT 'none',
    tags_written_at  TEXT
);

CREATE TABLE IF NOT EXISTS enrichment_candidates (
    id                INTEGER  PRIMARY KEY AUTOINCREMENT,
    track_id          INTEGER  NOT NULL REFERENCES tracks(id),
    source            TEXT     NOT NULL,
    suggested_artist  TEXT,
    suggested_album   TEXT,
    suggested_title   TEXT,
    suggested_year    TEXT,
    suggested_genre   TEXT,
    confidence        TEXT     NOT NULL,
    status            TEXT     NOT NULL DEFAULT 'pending_review',
    raw_response      TEXT,
    created_at        TEXT     NOT NULL
);

CREATE TABLE IF NOT EXISTS enrichment_runs (
    id                      INTEGER  PRIMARY KEY AUTOINCREMENT,
    started_at              TEXT     NOT NULL,
    last_processed_track_id INTEGER,
    tracks_processed        INTEGER  DEFAULT 0,
    tracks_remaining        INTEGER,
    daily_limit             INTEGER  NOT NULL,
    status                  TEXT     NOT NULL DEFAULT 'running'
);
"""
```

Update `_get_db()` to enable WAL:

```python
def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_config["db_path"])
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn
```

Add `_migrate_db()` after `_init_db()`:

```python
def _migrate_db() -> None:
    """Idempotently add new columns to existing tracks table."""
    with _get_db() as conn:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
        if "enrichment_status" not in existing:
            conn.execute(
                "ALTER TABLE tracks ADD COLUMN enrichment_status TEXT DEFAULT 'none'"
            )
        if "tags_written_at" not in existing:
            conn.execute("ALTER TABLE tracks ADD COLUMN tags_written_at TEXT")
```

Update `main()` to call `_migrate_db()` after `_init_db()`:

```python
    _init_db()
    _migrate_db()   # ← add this line
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_enrich_api.py -v
```
Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add id3_organiser.py tests/test_enrich_api.py
git commit -m "feat: add enrichment DB schema and WAL mode"
```

---

## Task 2: Heuristics Module

**Files:**
- Create: `enrichment/__init__.py`
- Create: `enrichment/heuristics.py`
- Create: `tests/test_heuristics.py`

- [ ] **Step 1: Create `enrichment/__init__.py`**

```python
# enrichment/__init__.py
```
(Empty — just marks as a package.)

- [ ] **Step 2: Write failing tests**

```python
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
        {"id": 2, "original_path": "/music/Beatles/t2.mp3", "artist": None, "album": None},
        {"id": 3, "original_path": "/music/Beatles/t3.mp3", "artist": None, "album": None},
    ]
    result = apply_folder_grouping(tracks)
    null_artist = [t for t in result if t["id"] in (2, 3)]
    assert all(t["artist"] == "The Beatles" for t in null_artist)


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
```

- [ ] **Step 3: Run to verify they fail**

```bash
python -m pytest tests/test_heuristics.py -v
```
Expected: FAIL — `enrichment.heuristics` not found.

- [ ] **Step 4: Implement `enrichment/heuristics.py`**

```python
# enrichment/heuristics.py
import re
from collections import Counter, defaultdict
from pathlib import Path

_NUMBERED = re.compile(r'^(\d{1,3})[.\s\-]+\s*(.+)$')
_ARTIST_TITLE = re.compile(r'^([^-]+?)\s+-\s+(.+)$')


def extract_from_path(original_path: str, source_dir: str) -> dict:
    """Parse folder structure and filename for tag hints."""
    path = Path(original_path)
    try:
        parts = path.relative_to(source_dir).parts
    except ValueError:
        parts = (path.name,)

    result = {
        "artist": None,
        "album": None,
        "title": None,
        "track_number": None,
        "confidence": "low",
    }

    # Folder-based signals (parts[-1] is filename)
    if len(parts) >= 3:
        result["artist"] = parts[-3]
        result["album"] = parts[-2]
        result["confidence"] = "high"
    elif len(parts) >= 2:
        result["artist"] = parts[-2]

    # Filename-based signals
    stem = path.stem
    m = _NUMBERED.match(stem)
    if m:
        result["track_number"] = f"{int(m.group(1)):02d}"
        result["title"] = m.group(2).strip()
    else:
        m = _ARTIST_TITLE.match(stem)
        if m and result["artist"] is None:
            result["artist"] = m.group(1).strip()
            result["title"] = m.group(2).strip()
        else:
            result["title"] = stem

    return result


def apply_folder_grouping(tracks: list[dict]) -> list[dict]:
    """Propagate most-common artist/album to siblings with null values.
    Only fills null fields. Requires ≥3 files per folder."""
    by_folder: dict[str, list[dict]] = defaultdict(list)
    for t in tracks:
        by_folder[str(Path(t["original_path"]).parent)].append(t)

    for folder_tracks in by_folder.values():
        if len(folder_tracks) < 3:
            continue

        for field in ("artist", "album"):
            values = [t[field] for t in folder_tracks if t.get(field)]
            if not values:
                continue
            most_common, count = Counter(values).most_common(1)[0]
            # Only propagate if present in majority of tracks
            if count >= len(folder_tracks) * 0.5:
                for t in folder_tracks:
                    if not t.get(field):
                        t[field] = most_common

    return tracks
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/test_heuristics.py -v
```
Expected: 8 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add enrichment/__init__.py enrichment/heuristics.py tests/test_heuristics.py
git commit -m "feat: add heuristics module for path/filename parsing"
```

---

## Task 3: Tag Writer Module

**Files:**
- Create: `enrichment/tag_writer.py`
- Create: `tests/test_tag_writer.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_tag_writer.py
import shutil
import pytest
from pathlib import Path
from mutagen import File as MutagenFile
from enrichment.tag_writer import write_tags


@pytest.fixture
def sample_mp3(tmp_path):
    """Copy a minimal MP3 fixture for tag writing tests."""
    src = Path("tests/fixtures/silent.mp3")
    dst = tmp_path / "track.mp3"
    if src.exists():
        shutil.copy(src, dst)
    else:
        # Create a minimal valid MP3 using mutagen if no fixture exists
        from mutagen.id3 import ID3, TIT2
        from mutagen.mp3 import MP3
        # Write a minimal header by creating empty ID3 tags
        import struct, os
        # Minimal MP3: ID3v2 header + one silent frame
        header = b"ID3\x03\x00\x00\x00\x00\x00\x00"
        # 32 bytes of a silent MPEG frame
        frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
        dst.write_bytes(header + frame)
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
    from enrichment.tag_writer import TagWriteError
    with pytest.raises(TagWriteError):
        write_tags("/nonexistent/path/track.mp3", {"artist": "A"})
```

- [ ] **Step 2: Create the MP3 test fixture directory**

```bash
mkdir -p tests/fixtures
```

- [ ] **Step 3: Run to verify tests fail**

```bash
python -m pytest tests/test_tag_writer.py -v
```
Expected: FAIL — `enrichment.tag_writer` not found.

- [ ] **Step 4: Implement `enrichment/tag_writer.py`**

```python
# enrichment/tag_writer.py
from mutagen import File as MutagenFile


class TagWriteError(Exception):
    pass


_EASY_FIELD_MAP = {
    "artist":       "artist",
    "album":        "album",
    "title":        "title",
    "year":         "date",
    "genre":        "genre",
    "track_number": "tracknumber",
}


def write_tags(file_path: str, tags: dict) -> None:
    """Write tag fields to an audio file using mutagen easy tags.

    Only writes keys present in tags dict with non-None values.
    Raises TagWriteError if the file cannot be opened or written.
    """
    try:
        audio = MutagenFile(file_path, easy=True)
    except Exception as exc:
        raise TagWriteError(f"Cannot open {file_path}: {exc}") from exc

    if audio is None:
        raise TagWriteError(f"Unsupported format or corrupt file: {file_path}")

    for field, value in tags.items():
        easy_key = _EASY_FIELD_MAP.get(field)
        if easy_key and value is not None:
            audio[easy_key] = [str(value)]

    try:
        audio.save()
    except Exception as exc:
        raise TagWriteError(f"Cannot save {file_path}: {exc}") from exc
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/test_tag_writer.py -v
```
Expected: 5 tests PASS (the `sample_mp3` fixture creates a minimal file inline if no fixture exists).

- [ ] **Step 6: Commit**

```bash
git add enrichment/tag_writer.py tests/test_tag_writer.py tests/fixtures/
git commit -m "feat: add tag writer module"
```

---

## Task 4: MusicBrainz Module

**Files:**
- Create: `enrichment/musicbrainz.py`
- Create: `tests/test_musicbrainz.py`

- [ ] **Step 1: Write failing tests**

```python
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
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_musicbrainz.py -v
```
Expected: FAIL — `enrichment.musicbrainz` not found.

- [ ] **Step 3: Implement `enrichment/musicbrainz.py`**

```python
# enrichment/musicbrainz.py
import time
from difflib import SequenceMatcher

import musicbrainzngs

musicbrainzngs.set_useragent("ID3MusicOrganiser", "1.0", "id3organiser@local")


class MusicBrainzError(Exception):
    pass


def query_musicbrainz(
    artist: str | None,
    album: str | None,
    title: str | None,
    retries: int = 3,
) -> dict | None:
    """Query MusicBrainz for a track. Returns tag dict or None.

    Confidence is 'high' when both title and artist match >85%, else 'low'.
    Retries up to `retries` times with exponential backoff on 503.
    """
    kwargs: dict = {"limit": 5}
    if title:
        kwargs["recording"] = title
    if artist:
        kwargs["artist"] = artist
    if album:
        kwargs["release"] = album

    for attempt in range(retries):
        try:
            result = musicbrainzngs.search_recordings(**kwargs)
            break
        except musicbrainzngs.ResponseError:
            return None
        except musicbrainzngs.NetworkError:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                return None

    recordings = result.get("recording-list", [])
    if not recordings:
        return None

    best = recordings[0]
    mb_title = best.get("title", "")
    mb_artist = best.get("artist-credit-phrase", "")
    releases = best.get("release-list", [])
    mb_album = releases[0].get("title", "") if releases else ""
    mb_date = releases[0].get("date", "") if releases else ""

    def _sim(a: str, b: str) -> float:
        return SequenceMatcher(None, a.lower(), b.lower()).ratio()

    title_score = _sim(title or "", mb_title)
    artist_score = _sim(artist or "", mb_artist)
    confidence = "high" if title_score > 0.85 and artist_score > 0.85 else "low"

    return {
        "artist": mb_artist or None,
        "album": mb_album or None,
        "title": mb_title or None,
        "year": mb_date[:4] if mb_date else None,
        "confidence": confidence,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_musicbrainz.py -v
```
Expected: 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add enrichment/musicbrainz.py tests/test_musicbrainz.py
git commit -m "feat: add MusicBrainz query module"
```

---

## Task 5: Claude Fallback Module

**Files:**
- Create: `enrichment/claude_fallback.py`
- Create: `tests/test_claude_fallback.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_claude_fallback.py
import json
from unittest.mock import patch, MagicMock
import pytest
from enrichment.claude_fallback import query_claude


def _mock_response(payload: dict):
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps(payload))]
    return msg


def test_returns_suggestion_with_low_confidence():
    payload = {"artist": "Pink Floyd", "album": "Animals", "title": "Dogs",
               "year": "1977", "confidence": "high"}
    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.return_value = _mock_response(payload)
        result = query_claude("Dogs.mp3", "/music/Pink Floyd/Animals",
                              {}, ["Pigs.mp3", "Sheep.mp3"])
    # Claude confidence is always downgraded to 'low'
    assert result["confidence"] == "low"
    assert result["artist"] == "Pink Floyd"


def test_returns_none_on_api_error():
    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.side_effect = Exception("API error")
        result = query_claude("track.mp3", "/music/Unknown", {}, [])
    assert result is None


def test_returns_none_on_invalid_json():
    with patch("anthropic.Anthropic") as MockClient:
        bad = MagicMock()
        bad.content = [MagicMock(text="not json at all")]
        MockClient.return_value.messages.create.return_value = bad
        result = query_claude("track.mp3", "/music/Unknown", {}, [])
    assert result is None


def test_prompt_includes_neighbours():
    payload = {"artist": "A", "album": "B", "title": "C", "year": "2000", "confidence": "low"}
    captured_prompt = []

    def capture(*args, **kwargs):
        captured_prompt.append(kwargs.get("messages", [{}])[0].get("content", ""))
        return _mock_response(payload)

    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.side_effect = capture
        query_claude("track.mp3", "/music/Artist", {}, ["neighbour1.mp3", "neighbour2.mp3"])

    assert "neighbour1.mp3" in captured_prompt[0]
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_claude_fallback.py -v
```
Expected: FAIL — `enrichment.claude_fallback` not found.

- [ ] **Step 3: Implement `enrichment/claude_fallback.py`**

```python
# enrichment/claude_fallback.py
import json

import anthropic


def query_claude(
    filename: str,
    folder_path: str,
    partial_tags: dict,
    neighbours: list[str],
) -> dict | None:
    """Ask Claude to identify track metadata from filename and context.

    Always returns confidence='low' regardless of Claude's own assessment,
    so all results go to the review queue.
    Returns None on any API or parse error.
    """
    neighbour_str = ", ".join(neighbours[:10]) if neighbours else "none"
    partial_str = json.dumps(
        {k: v for k, v in partial_tags.items() if v}, indent=None
    )

    prompt = (
        "You are a music metadata expert. Given a music file's name, folder, "
        "and neighbouring files, identify the artist, album, title, and year.\n\n"
        f"File: {filename}\n"
        f"Folder: {folder_path}\n"
        f"Partial tags: {partial_str}\n"
        f"Neighbouring files: {neighbour_str}\n\n"
        'Reply with ONLY valid JSON in this exact format (use null for unknown):\n'
        '{"artist": "...", "album": "...", "title": "...", "year": "...", '
        '"confidence": "high" or "low"}'
    )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        data = json.loads(response.content[0].text)
    except (json.JSONDecodeError, KeyError, IndexError):
        return None
    except Exception:
        return None

    # Always treat as low confidence — all Claude results go to review queue
    data["confidence"] = "low"
    return data
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_claude_fallback.py -v
```
Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add enrichment/claude_fallback.py tests/test_claude_fallback.py
git commit -m "feat: add Claude fallback module"
```

---

## Task 6: Pipeline Module

**Files:**
- Create: `enrichment/pipeline.py`
- Create: `tests/test_pipeline.py`

- [ ] **Step 1: Write failing tests**

```python
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
            status TEXT NOT NULL DEFAULT 'running'
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


def test_stage1_creates_candidate_for_high_confidence(db, tmp_path):
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
    # High confidence (Artist/Album from path) → auto-applied → enriched
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
         patch("enrichment.claude_fallback.query_claude", return_value=None):
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
         patch("enrichment.claude_fallback.query_claude", return_value=None):
        run_stage2_batch(db, cfg, daily_limit=10, claude_limit=5)

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT status FROM enrichment_runs ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    assert row[0] == "complete"
```

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_pipeline.py -v
```
Expected: FAIL — `enrichment.pipeline` not found.

- [ ] **Step 3: Implement `enrichment/pipeline.py`**

```python
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
    album = meta.get("album")
    title = meta.get("title")
    ext = Path(original_path).suffix.lower()

    if artist and album and title:
        tn = _parse_track_number(meta.get("track_number"))
        prefix = f"{tn}. " if tn else ""
        filename = _sanitize(f"{prefix}{title}") + ext
        dest = Path(cfg["processed_base"]) / _sanitize(artist) / _sanitize(album) / filename
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
    """Run heuristic enrichment over all unmatched tracks with enrichment_status='none'."""
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
            "album":        track["album"] or suggestion.get("album"),
            "title":        track["title"] or suggestion.get("title"),
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
            conn = _get_conn(db_path)
            conn.execute(
                "INSERT OR IGNORE INTO enrichment_candidates "
                "(track_id, source, suggested_artist, suggested_album, "
                "suggested_title, confidence, status, created_at) "
                "VALUES (?, 'heuristic', ?, ?, ?, ?, 'pending_review', ?)",
                (track["id"], merged.get("artist"), merged.get("album"),
                 merged.get("title"), confidence, now)
            )
            if merged.get("artist") or merged.get("album") or merged.get("title"):
                conn.execute(
                    "UPDATE tracks SET enrichment_status='review_needed' WHERE id=?",
                    (track["id"],)
                )
            conn.commit()
            conn.close()


def run_stage2_batch(db_path: str, cfg: dict, daily_limit: int, claude_limit: int) -> None:
    """Process up to daily_limit tracks via MusicBrainz, then Claude for remaining."""
    from .musicbrainz import query_musicbrainz
    from .claude_fallback import query_claude

    now = datetime.now().isoformat()
    conn = _get_conn(db_path)
    conn.execute(
        "INSERT INTO enrichment_runs (started_at, daily_limit, status) VALUES (?, ?, 'running')",
        (now, daily_limit)
    )
    conn.commit()
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Tracks still needing enrichment: unmatched and not already enriched
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
    claude_processed = 0

    for track in tracks:
        # Gather neighbours for context
        folder = str(Path(track["original_path"]).parent)
        conn = _get_conn(db_path)
        neighbours = [
            r[0] for r in conn.execute(
                "SELECT filename FROM tracks WHERE original_path LIKE ? AND id != ? LIMIT 15",
                (f"{folder}%", track["id"])
            ).fetchall()
        ]
        conn.close()

        result = None
        source = None

        # Stage 2: MusicBrainz
        if mb_processed < daily_limit:
            result = query_musicbrainz(
                track.get("artist"), track.get("album"), track.get("title")
                or Path(track["original_path"]).stem
            )
            source = "musicbrainz"
            mb_processed += 1
            time.sleep(1.1)  # MusicBrainz rate limit: 1 req/sec

        # Stage 3: Claude fallback if MusicBrainz found nothing
        if result is None and claude_processed < claude_limit:
            partial = {k: track.get(k) for k in ("artist", "album", "title")}
            result = query_claude(
                track["filename"],
                str(Path(track["original_path"]).parent),
                partial,
                neighbours,
            )
            source = "claude"
            claude_processed += 1

        if result and source:
            conn = _get_conn(db_path)
            conn.execute(
                "INSERT INTO enrichment_candidates "
                "(track_id, source, suggested_artist, suggested_album, "
                "suggested_title, suggested_year, confidence, raw_response, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending_review', ?)",
                (track["id"], source, result.get("artist"), result.get("album"),
                 result.get("title"), result.get("year"), result.get("confidence", "low"),
                 json.dumps(result), now)
            )
            conn.execute(
                "UPDATE tracks SET enrichment_status='review_needed' WHERE id=?",
                (track["id"],)
            )
            conn.commit()
            conn.close()

    conn = _get_conn(db_path)
    conn.execute(
        "UPDATE enrichment_runs SET tracks_processed=?, status='complete' WHERE id=?",
        (mb_processed + claude_processed, run_id)
    )
    conn.commit()
    conn.close()


class EnrichmentScheduler:
    """Long-lived scheduler that runs Stage 2/3 batches daily."""

    def __init__(self, db_path: str, cfg: dict, daily_limit: int, claude_limit: int):
        self.db_path = db_path
        self.cfg = cfg
        self.daily_limit = daily_limit
        self.claude_limit = claude_limit
        self._run_now_event = threading.Event()
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
            self._status["tracks_remaining"] = row[0] if row else 0
        except Exception:
            pass

    def _run_batch(self) -> None:
        self._status["running"] = True
        self._status["last_run"] = datetime.now().isoformat()
        self._status["tracks_processed_today"] = 0
        try:
            run_stage2_batch(self.db_path, self.cfg, self.daily_limit, self.claude_limit)
            self._status["tracks_processed_today"] = self.daily_limit
        finally:
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_pipeline.py -v
```
Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add enrichment/pipeline.py tests/test_pipeline.py
git commit -m "feat: add enrichment pipeline and daily scheduler"
```

---

## Task 7: `enrich` CLI Subcommand

**Files:**
- Modify: `id3_organiser.py`

- [ ] **Step 1: Write failing test**

```python
# Add to tests/test_enrich_api.py

from click.testing import CliRunner
from id3_organiser import enrich


def test_enrich_command_exists():
    runner = CliRunner()
    result = runner.invoke(enrich, ["--help"])
    assert result.exit_code == 0
    assert "--daily-limit" in result.output
    assert "--run-now" in result.output
```

- [ ] **Step 2: Run to verify it fails**

```bash
python -m pytest tests/test_enrich_api.py::test_enrich_command_exists -v
```
Expected: FAIL — `enrich` not importable from `id3_organiser`.

- [ ] **Step 3: Add `enrich` command to `id3_organiser.py`**

Add the import at the top of the file (after existing imports):

```python
from enrichment.pipeline import EnrichmentScheduler
```

Add a module-level global for the scheduler (after `_move_event_queue`):

```python
_enrichment_scheduler: "EnrichmentScheduler | None" = None
```

Add the `enrich` Click command before `if __name__ == "__main__":`:

```python
@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("source_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--db",           default="music_organiser.db", show_default=True,
              help="SQLite database path.")
@click.option("--processed",    default="./processed",         show_default=True,
              help="Base directory for organised files.")
@click.option("--unmatched",    default="./unmatched",         show_default=True,
              help="Base directory for unmatched files.")
@click.option("--daily-limit",  default=500,                   show_default=True,
              help="Max MusicBrainz lookups per day.")
@click.option("--claude-limit", default=100,                   show_default=True,
              help="Max Claude AI lookups per day.")
@click.option("--run-now",      is_flag=True,
              help="Run a batch immediately without waiting for daily schedule.")
def enrich(source_dir: str, db: str, processed: str, unmatched: str,
           daily_limit: int, claude_limit: int, run_now: bool) -> None:
    """Run the tag enrichment pipeline for unmatched tracks.

    SOURCE_DIR  Root directory (same as used with the main command).
    """
    global _enrichment_scheduler
    _config["db_path"]        = db
    _config["source_dir"]     = os.path.abspath(source_dir)
    _config["processed_base"] = os.path.abspath(processed)
    _config["unmatched_base"] = os.path.abspath(unmatched)

    _init_db()
    _migrate_db()

    cfg = {
        "source_dir":     _config["source_dir"],
        "processed_base": _config["processed_base"],
        "unmatched_base": _config["unmatched_base"],
    }

    console.print()
    console.print(
        Panel.fit(
            Text.assemble(
                ("  ID3 Enrichment  ", "bold white on #4c1d95"),
                (f"  daily limit: {daily_limit}  ", "bold #a78bfa on #4c1d95"),
            ),
            border_style="#7c3aed",
            padding=(0, 2),
        )
    )
    console.print()

    _enrichment_scheduler = EnrichmentScheduler(db, cfg, daily_limit, claude_limit)

    if run_now:
        console.print("[bold cyan]Running immediate batch…[/bold cyan]")
        _enrichment_scheduler.run_now()

    console.print(
        "[dim]Stage 1 (heuristics) running now. "
        "Stage 2/3 scheduled daily at 02:00. Press Ctrl+C to stop.[/dim]"
    )
    _enrichment_scheduler.start()

    # Keep process alive
    try:
        import time
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        console.print("\n[yellow]Enrichment stopped.[/yellow]")
```

Also register it in a `cli` group. Replace the final `if __name__ == "__main__":` block:

```python
@click.group()
def cli():
    pass


cli.add_command(main, name="organise")
cli.add_command(enrich, name="enrich")


if __name__ == "__main__":
    cli()
```

> **Note:** This wraps both commands under a group. The original `main` command is now `python id3_organiser.py organise <source_dir>`. Update any existing launch scripts (`launch.ps1`) to use `organise` subcommand.

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_enrich_api.py::test_enrich_command_exists -v
```
Expected: PASS.

- [ ] **Step 5: Verify existing tests still pass**

```bash
python -m pytest tests/ -v
```
Expected: All previously passing tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add id3_organiser.py
git commit -m "feat: add enrich CLI subcommand"
```

---

## Task 8: Flask API Endpoints

**Files:**
- Modify: `id3_organiser.py`
- Modify: `tests/test_enrich_api.py`

- [ ] **Step 1: Write failing tests**

```python
# Add to tests/test_enrich_api.py

import json as _json
import sqlite3 as _sqlite3


@pytest.fixture
def client_with_candidate(tmp_path):
    db = str(tmp_path / "test.db")
    app_module._config["db_path"] = db
    app_module._config["source_dir"] = str(tmp_path)
    app_module._config["processed_base"] = str(tmp_path / "processed")
    app_module._config["unmatched_base"] = str(tmp_path / "unmatched")
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
    assert r.status_code == 200


def test_post_apply_writes_tags_and_updates_db(client_with_candidate, tmp_path):
    c, track_id, db_path = client_with_candidate

    # Create a fake audio file so mutagen doesn't fail
    fake_file = tmp_path / "01 - Song.mp3"
    fake_file.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" + b"\x00" * 413)

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

    conn = _sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT enrichment_status FROM tracks WHERE id=?", (track_id,)
    ).fetchone()
    conn.close()
    assert row[0] == "enriched"
```

Add `import unittest.mock` at the top of `tests/test_enrich_api.py`.

- [ ] **Step 2: Run to verify they fail**

```bash
python -m pytest tests/test_enrich_api.py -k "candidates or enrich_status or run_now or apply" -v
```
Expected: FAIL — routes not defined.

- [ ] **Step 3: Add the five new Flask routes to `id3_organiser.py`**

Add after the existing `api_move_stream` route:

```python
# ─────────────────────────────────────────────────────────────────────────────
# Enrichment API
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/enrich/candidates")
def api_enrich_candidates():
    status     = request.args.get("status", "pending_review")
    source     = request.args.get("source", "all")
    confidence = request.args.get("confidence", "all")
    page       = max(1, int(request.args.get("page", 1)))
    per        = min(200, max(10, int(request.args.get("per", 50))))

    sql    = """
        SELECT ec.*, t.original_path, t.filename
        FROM enrichment_candidates ec
        JOIN tracks t ON t.id = ec.track_id
        WHERE 1=1
    """
    params: list = []

    if status != "all":
        sql += " AND ec.status = ?";     params.append(status)
    if source != "all":
        sql += " AND ec.source = ?";     params.append(source)
    if confidence != "all":
        sql += " AND ec.confidence = ?"; params.append(confidence)

    sql += " ORDER BY ec.confidence DESC, ec.created_at DESC"

    with _get_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
        rows  = conn.execute(
            sql + f" LIMIT {per} OFFSET {(page - 1) * per}", params
        ).fetchall()

    return jsonify({"total": total, "page": page, "per": per,
                    "candidates": [dict(r) for r in rows]})


@app.route("/api/enrich/status")
def api_enrich_status():
    if _enrichment_scheduler is not None:
        return jsonify(_enrichment_scheduler.get_status())

    with _get_db() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE matched=0 "
            "AND enrichment_status NOT IN ('enriched')"
        ).fetchone()[0]
    return jsonify({
        "running": False,
        "tracks_processed_today": 0,
        "tracks_remaining": remaining,
        "next_run": "not started",
        "last_run": None,
    })


@app.route("/api/enrich/run-now", methods=["POST"])
def api_enrich_run_now():
    if _enrichment_scheduler is not None:
        _enrichment_scheduler.run_now()
        return jsonify({"ok": True, "message": "batch triggered"})
    return jsonify({"ok": False, "message": "enrichment scheduler not running"}), 400


_apply_event_queue: queue.Queue = queue.Queue()


@app.route("/api/enrich/apply", methods=["POST"])
def api_enrich_apply():
    global _apply_event_queue
    _apply_event_queue = queue.Queue()

    from enrichment.tag_writer import write_tags, TagWriteError

    data          = request.json or {}
    candidate_ids = data.get("candidate_ids", [])
    if not candidate_ids:
        return jsonify({"error": "no candidate_ids provided"}), 400

    with _get_db() as conn:
        ph   = ",".join("?" * len(candidate_ids))
        rows = conn.execute(
            f"SELECT ec.*, t.original_path FROM enrichment_candidates ec "
            f"JOIN tracks t ON t.id = ec.track_id "
            f"WHERE ec.id IN ({ph}) AND ec.status = 'pending_review'",
            candidate_ids,
        ).fetchall()
    candidates = [dict(r) for r in rows]

    def _do_apply():
        applied = failed = 0
        for i, cand in enumerate(candidates, 1):
            tags = {
                "artist":       cand["suggested_artist"],
                "album":        cand["suggested_album"],
                "title":        cand["suggested_title"],
                "year":         cand["suggested_year"],
                "genre":        cand.get("suggested_genre"),
            }
            tid  = cand["track_id"]
            cid  = cand["id"]
            src  = cand["original_path"]

            _apply_event_queue.put({
                "type": "progress", "id": cid, "current": i,
                "total": len(candidates), "filename": cand.get("filename", ""),
                "status": "applying",
            })

            try:
                write_tags(src, tags)
                meta = {**tags, "track_number": None}
                dest, is_matched = _calculate_destination(meta, src)
                now = datetime.now().isoformat()
                with _get_db() as conn:
                    conn.execute(
                        "UPDATE tracks SET artist=?, album=?, title=?, year=?, "
                        "destination_path=?, matched=?, enrichment_status='enriched', "
                        "tags_written_at=? WHERE id=?",
                        (tags["artist"], tags["album"], tags["title"], tags.get("year"),
                         dest, 1 if is_matched else 0, now, tid)
                    )
                    conn.execute(
                        "UPDATE enrichment_candidates SET status='applied' WHERE id=?", (cid,)
                    )
                applied += 1
                _apply_event_queue.put({
                    "type": "progress", "id": cid, "current": i,
                    "total": len(candidates), "status": "applied",
                })
            except Exception as exc:
                failed += 1
                with _get_db() as conn:
                    conn.execute(
                        "UPDATE tracks SET error_message=? WHERE id=?", (str(exc), tid)
                    )
                _apply_event_queue.put({
                    "type": "progress", "id": cid, "current": i,
                    "total": len(candidates), "status": "error", "message": str(exc),
                })

        _apply_event_queue.put({"type": "complete", "applied": applied, "failed": failed})

    threading.Thread(target=_do_apply, daemon=True).start()
    return jsonify({"status": "started", "total": len(candidates)})


@app.route("/api/enrich/apply/stream")
def api_enrich_apply_stream():
    def _generate():
        while True:
            try:
                event = _apply_event_queue.get(timeout=60)
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_enrich_api.py -v
```
Expected: All tests PASS.

- [ ] **Step 5: Run full test suite**

```bash
python -m pytest tests/ -v
```
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add id3_organiser.py tests/test_enrich_api.py
git commit -m "feat: add enrichment Flask API endpoints"
```

---

## Task 9: Web UI — Enrich Tab

**Files:**
- Modify: `templates/index.html`

The Enrich tab requires three additions to the existing Bootstrap-based UI: a nav tab, tab panel content (status bar + review queue table), and an enrichment_status filter on the existing track list. No new JS files — follows the existing inline script pattern.

- [ ] **Step 1: Add CSS for Enrich tab to the `<style>` block**

Find the line `/* ── Progress modal ──` in `templates/index.html` and insert before it:

```css
    /* ── Enrich badges ──────────────────────────────────────────── */
    .badge-heuristic { background: #1e3a5f; color: #93c5fd; }
    .badge-musicbrainz { background: #1a3a2a; color: #86efac; }
    .badge-claude { background: #3a1a5f; color: #c4b5fd; }
    .badge-high { background: #14532d; color: #4ade80; }
    .badge-low  { background: #713f12; color: #fde68a; }

    /* ── Enrich status bar ───────────────────────────────────────── */
    .enrich-status-bar {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: .5rem;
      padding: .7rem 1.2rem;
      display: flex;
      gap: 1.5rem;
      align-items: center;
      flex-wrap: wrap;
      margin-bottom: 1rem;
    }
    .enrich-stat { text-align: center; }
    .enrich-stat .val { font-size: 1.3rem; font-weight: 700; color: var(--accent-light); }
    .enrich-stat .lbl { font-size: .68rem; color: #64748b; text-transform: uppercase; }

    /* ── Inline editable cells ───────────────────────────────────── */
    .editable-cell {
      background: transparent;
      border: 1px solid transparent;
      border-radius: .25rem;
      color: #cbd5e1;
      font-size: .8rem;
      padding: .15rem .3rem;
      width: 100%;
    }
    .editable-cell:focus {
      outline: none;
      border-color: var(--accent);
      background: #1a1040;
    }
```

- [ ] **Step 2: Add the Enrich nav tab**

Find the nav tabs section in `index.html`. It will look something like a `<ul class="nav nav-tabs">` block. Add the Enrich tab item:

```html
<li class="nav-item">
  <a class="nav-link" id="enrich-tab" data-bs-toggle="tab" href="#enrichPane" role="tab">
    <i class="bi bi-stars"></i> Enrich
    <span class="badge bg-warning text-dark ms-1" id="enrichBadge" style="display:none"></span>
  </a>
</li>
```

- [ ] **Step 3: Add the Enrich tab pane**

Add this inside the `.tab-content` div, alongside the existing track list pane:

```html
<div class="tab-pane fade" id="enrichPane" role="tabpanel">
  <!-- Status Bar -->
  <div class="p-3">
    <div class="enrich-status-bar" id="enrichStatusBar">
      <div class="enrich-stat">
        <div class="val" id="esRemaining">—</div>
        <div class="lbl">Remaining</div>
      </div>
      <div class="enrich-stat">
        <div class="val" id="esProcessedToday">—</div>
        <div class="lbl">Today</div>
      </div>
      <div class="enrich-stat">
        <div class="val" id="esNextRun">—</div>
        <div class="lbl">Next Run</div>
      </div>
      <div class="ms-auto d-flex gap-2 align-items-center">
        <span id="esRunning" class="badge bg-success" style="display:none">Running</span>
        <button class="btn btn-sm btn-outline-primary" onclick="triggerRunNow()">
          <i class="bi bi-play-fill"></i> Run Now
        </button>
      </div>
    </div>

    <!-- Filters -->
    <div class="filter-bar mb-2">
      <select class="form-select form-select-sm" style="width:auto" id="ecSourceFilter" onchange="loadCandidates()">
        <option value="all">All sources</option>
        <option value="heuristic">Heuristic</option>
        <option value="musicbrainz">MusicBrainz</option>
        <option value="claude">Claude</option>
      </select>
      <select class="form-select form-select-sm" style="width:auto" id="ecConfFilter" onchange="loadCandidates()">
        <option value="all">All confidence</option>
        <option value="high">High</option>
        <option value="low">Low</option>
      </select>
      <button class="btn btn-sm btn-success ms-auto" onclick="approveSelected()">
        <i class="bi bi-check2-all"></i> Write Tags
      </button>
      <button class="btn btn-sm btn-outline-danger" onclick="rejectSelected()">
        <i class="bi bi-x-lg"></i> Reject
      </button>
    </div>

    <!-- Review Queue Table -->
    <div class="table-wrap">
      <table class="tracks-table" id="candidatesTable">
        <thead>
          <tr>
            <th><input type="checkbox" id="ecSelectAll" onchange="toggleAllCandidates(this)"></th>
            <th>File</th>
            <th>Artist</th>
            <th>Album</th>
            <th>Title</th>
            <th>Year</th>
            <th>Source</th>
            <th>Confidence</th>
          </tr>
        </thead>
        <tbody id="candidatesTbody"></tbody>
      </table>
    </div>

    <!-- Pagination -->
    <div class="pagination-bar" id="ecPagination"></div>

    <!-- Apply progress log -->
    <div id="applyLog" style="display:none" class="mt-3">
      <div id="applyLogContent" style="max-height:200px;overflow-y:auto;font-family:monospace;font-size:.78rem;background:#0d0d12;border:1px solid var(--border);border-radius:.35rem;padding:.6rem .8rem;"></div>
    </div>
  </div>
</div>
```

- [ ] **Step 4: Add enrichment_status filter to the existing track list filter bar**

Find the existing filter bar in `index.html` (the `.filter-bar` with status/match selects) and add:

```html
<select class="form-select form-select-sm" style="width:auto" id="enrichFilter" onchange="loadTracks()">
  <option value="all">All enrichment</option>
  <option value="none">Not enriched</option>
  <option value="review_needed">Review needed</option>
  <option value="enriched">Enriched</option>
</select>
```

Also update the existing `loadTracks()` JS function to include this param:

```javascript
// Find the line that builds the API URL in loadTracks() and add:
const enrichFilter = document.getElementById('enrichFilter')?.value || 'all';
// Add to the URL params:
if (enrichFilter !== 'all') params.set('enrichment', enrichFilter);
```

And update `/api/tracks` in `id3_organiser.py` to handle the new filter:

```python
# In api_tracks(), after the existing filters:
enrich_filter = request.args.get("enrichment", "all")
if enrich_filter != "all":
    sql += " AND enrichment_status = ?"; params.append(enrich_filter)
```

- [ ] **Step 5: Add Enrich JS to the inline `<script>` block in `index.html`**

```javascript
// ── Enrich tab ────────────────────────────────────────────────────────────

let ecPage = 1;

async function loadEnrichStatus() {
  const r = await fetch('/api/enrich/status');
  const d = await r.json();
  document.getElementById('esRemaining').textContent = d.tracks_remaining ?? '—';
  document.getElementById('esProcessedToday').textContent = d.tracks_processed_today ?? '—';
  document.getElementById('esNextRun').textContent = d.next_run ?? '—';
  const running = document.getElementById('esRunning');
  running.style.display = d.running ? '' : 'none';
}

async function loadCandidates(page = 1) {
  ecPage = page;
  const source = document.getElementById('ecSourceFilter').value;
  const conf   = document.getElementById('ecConfFilter').value;
  const params = new URLSearchParams({ page, per: 50, source, confidence: conf });
  const r = await fetch('/api/enrich/candidates?' + params);
  const d = await r.json();

  const badge = document.getElementById('enrichBadge');
  if (d.total > 0) { badge.textContent = d.total; badge.style.display = ''; }
  else badge.style.display = 'none';

  const tbody = document.getElementById('candidatesTbody');
  tbody.innerHTML = d.candidates.map(c => `
    <tr data-id="${c.id}" data-track-id="${c.track_id}">
      <td><input type="checkbox" class="ec-check" value="${c.id}"></td>
      <td class="ellipsis" title="${c.original_path}">${c.filename}</td>
      <td><input class="editable-cell" data-field="suggested_artist" value="${c.suggested_artist || ''}"></td>
      <td><input class="editable-cell" data-field="suggested_album"  value="${c.suggested_album  || ''}"></td>
      <td><input class="editable-cell" data-field="suggested_title"  value="${c.suggested_title  || ''}"></td>
      <td><input class="editable-cell" data-field="suggested_year"   value="${c.suggested_year   || ''}" style="width:60px"></td>
      <td><span class="badge badge-${c.source}">${c.source}</span></td>
      <td><span class="badge badge-${c.confidence}">${c.confidence}</span></td>
    </tr>
  `).join('');

  const total_pages = Math.ceil(d.total / 50);
  document.getElementById('ecPagination').innerHTML =
    `<span>${d.total} candidates</span>` +
    (total_pages > 1 ? `<div class="d-flex gap-1">
      ${ecPage > 1 ? `<button class="btn btn-sm btn-outline-secondary" onclick="loadCandidates(${ecPage-1})">Prev</button>` : ''}
      <span class="px-2 py-1">Page ${ecPage}/${total_pages}</span>
      ${ecPage < total_pages ? `<button class="btn btn-sm btn-outline-secondary" onclick="loadCandidates(${ecPage+1})">Next</button>` : ''}
    </div>` : '');
}

function toggleAllCandidates(cb) {
  document.querySelectorAll('.ec-check').forEach(c => c.checked = cb.checked);
}

function getSelectedCandidateIds() {
  return [...document.querySelectorAll('.ec-check:checked')].map(c => parseInt(c.value));
}

async function approveSelected() {
  const ids = getSelectedCandidateIds();
  if (!ids.length) { alert('Select at least one candidate.'); return; }

  const log = document.getElementById('applyLog');
  const logContent = document.getElementById('applyLogContent');
  log.style.display = '';
  logContent.innerHTML = '';

  const r = await fetch('/api/enrich/apply', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ candidate_ids: ids }),
  });
  const d = await r.json();
  if (!r.ok) { alert('Error: ' + d.error); return; }

  const es = new EventSource('/api/enrich/apply/stream');
  es.onmessage = e => {
    const ev = JSON.parse(e.data);
    if (ev.type === 'heartbeat') return;
    const cls = ev.status === 'applied' ? 'log-ok' : ev.status === 'error' ? 'log-err' : 'log-info';
    logContent.innerHTML += `<div class="${cls}">[${ev.current}/${ev.total}] ${ev.status}: ${ev.filename || ''} ${ev.message || ''}</div>`;
    logContent.scrollTop = logContent.scrollHeight;
    if (ev.type === 'complete') {
      es.close();
      loadCandidates(ecPage);
      loadEnrichStatus();
    }
  };
}

async function rejectSelected() {
  const ids = getSelectedCandidateIds();
  if (!ids.length) return;
  for (const id of ids) {
    await fetch(`/api/enrich/candidates/${id}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ status: 'rejected' }),
    });
  }
  loadCandidates(ecPage);
}

async function triggerRunNow() {
  await fetch('/api/enrich/run-now', { method: 'POST' });
  loadEnrichStatus();
}

// Add PATCH endpoint for candidate status update
// Also poll enrich status every 30s when tab is active
document.getElementById('enrich-tab')?.addEventListener('shown.bs.tab', () => {
  loadCandidates();
  loadEnrichStatus();
});
```

- [ ] **Step 6: Add `PATCH /api/enrich/candidates/<id>` route to `id3_organiser.py`**

```python
@app.route("/api/enrich/candidates/<int:candidate_id>", methods=["PATCH"])
def api_enrich_candidate_update(candidate_id: int):
    data    = request.json or {}
    allowed = {"status", "suggested_artist", "suggested_album",
               "suggested_title", "suggested_year"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({"error": "nothing to update"}), 400
    set_sql = ", ".join(f"{k} = ?" for k in updates)
    values  = list(updates.values()) + [candidate_id]
    with _get_db() as conn:
        conn.execute(
            f"UPDATE enrichment_candidates SET {set_sql} WHERE id = ?", values
        )
    return jsonify({"ok": True})
```

- [ ] **Step 7: Run full test suite**

```bash
python -m pytest tests/ -v
```
Expected: All tests PASS.

- [ ] **Step 8: Manual smoke test**

```bash
python id3_organiser.py organise /path/to/music --no-browser
# In another terminal:
python id3_organiser.py enrich /path/to/music --run-now
# Open http://localhost:5000, click Enrich tab
```
Verify: status bar shows remaining count, candidates appear in table, inline edits work, "Write Tags" button triggers SSE progress.

- [ ] **Step 9: Commit**

```bash
git add templates/index.html id3_organiser.py
git commit -m "feat: add Enrich tab to web UI with review queue and status bar"
```

---

## Task 10: Dependencies

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Update `requirements.txt`**

```
mutagen>=1.47.0
rich>=13.7.0
click>=8.1.0
flask>=3.0.0
pytest>=8.0.0
musicbrainzngs>=0.7.1
anthropic>=0.25.0
schedule>=1.2.0
```

- [ ] **Step 2: Install and verify**

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
```
Expected: All tests PASS.

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "chore: add enrichment dependencies"
```

---

## Spec Coverage Checklist

| Spec requirement | Task(s) |
|---|---|
| `enrichment_candidates` table | Task 1 |
| `enrichment_runs` table | Task 1 |
| `enrichment_status` + `tags_written_at` on `tracks` | Task 1 |
| WAL mode | Task 1 |
| Stage 1: path/filename heuristics | Task 2 |
| Stage 1: same-folder grouping (null-only propagation) | Task 2 |
| Stage 1: high confidence auto-apply | Task 6 |
| Stage 1: low confidence → pending_review | Task 6 |
| Tag write-back via mutagen | Task 3 |
| TagWriteError on missing file | Task 3 |
| Stage 2: MusicBrainz API query | Task 4 |
| Stage 2: confidence scoring (>85% = high) | Task 4 |
| Stage 2: retry on NetworkError + rate-limit sleep | Task 4 |
| Stage 3: Claude AI fallback (always low confidence) | Task 5 |
| Stage 3: --claude-limit cap | Task 6 |
| Daily scheduler thread | Task 6 |
| `EnrichmentScheduler.run_now()` | Task 6 |
| `enrich` CLI subcommand + `--daily-limit`, `--run-now`, `--claude-limit` | Task 7 |
| `GET /api/enrich/candidates` | Task 8 |
| `POST /api/enrich/apply` + SSE stream | Task 8 |
| `GET /api/enrich/status` | Task 8 |
| `POST /api/enrich/run-now` | Task 8 |
| `PATCH /api/enrich/candidates/<id>` | Task 9 |
| Enrich tab: review queue with inline editing | Task 9 |
| Enrich tab: status bar + Run Now | Task 9 |
| enrichment_status filter on track list | Task 9 |
| Approve / Reject bulk actions | Task 9 |
| New dependencies | Task 10 |
