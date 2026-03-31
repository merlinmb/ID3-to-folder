# CSV Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `GET /api/export/csv` Flask route and a matching "Export CSV" button in the web UI that downloads all tracks as a CSV file.

**Architecture:** A single new Flask route queries all rows from the `tracks` table and streams them back as a `text/csv` response with a `Content-Disposition: attachment` header. The frontend adds one button to the action bar that sets `window.location` to trigger the native browser download.

**Tech Stack:** Python stdlib (`csv`, `io`), Flask, pytest + Flask test client

---

## File Map

| File | Change |
|------|--------|
| `id3_organiser.py` | Add `GET /api/export/csv` route |
| `templates/index.html` | Add Export CSV button to action bar |
| `tests/test_csv_export.py` | New — integration tests for the route |

---

### Task 1: Set up test infrastructure and write failing test for the CSV export route

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_csv_export.py`

- [ ] **Step 1: Add pytest to requirements**

Append to `requirements.txt`:
```
pytest>=8.0.0
```

Install it:
```bash
pip install pytest
```

- [ ] **Step 2: Create the tests package**

Create `tests/__init__.py` as an empty file.

- [ ] **Step 3: Write the failing tests**

Create `tests/test_csv_export.py`:

```python
import csv
import io
import sqlite3
import tempfile
import os
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
```

- [ ] **Step 4: Run the tests — confirm they all fail**

```bash
cd "e:/GoogleDrive/Arduino/ID3-to-folder"
python -m pytest tests/test_csv_export.py -v
```

Expected: FAIL — `AttributeError` or `404` because the route doesn't exist yet.

- [ ] **Step 5: Commit the failing tests**

```bash
git add requirements.txt tests/__init__.py tests/test_csv_export.py
git commit -m "test: add failing tests for CSV export route"
```

---

### Task 2: Implement the `/api/export/csv` Flask route

**Files:**
- Modify: `id3_organiser.py` (add route after `api_stats`, around line 365)

- [ ] **Step 1: Add `csv` and `io` imports**

At the top of `id3_organiser.py`, the existing imports block starts at line 13. Add `csv` and `io` to the stdlib imports:

```python
import csv
import io
import json
import os
import queue
import re
import shutil
import sqlite3
import threading
import webbrowser
```

- [ ] **Step 2: Add the route**

Insert the following route after the `api_stats` function (after line 365, before `@app.route("/api/move"`):

```python
@app.route("/api/export/csv")
def api_export_csv():
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT filename, artist, album, track_number, title, genre, year, "
            "duration, original_path, destination_path FROM tracks "
            "ORDER BY artist NULLS LAST, album NULLS LAST, track_number, title"
        ).fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Filename", "Artist", "Album", "Track Number",
        "Title", "Genre", "Year", "Duration",
        "Original Path", "Destination Path",
    ])
    for row in rows:
        writer.writerow([v if v is not None else "" for v in row])

    return app.response_class(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="music_library.csv"'},
    )
```

- [ ] **Step 3: Run the tests — confirm they all pass**

```bash
cd "e:/GoogleDrive/Arduino/ID3-to-folder"
python -m pytest tests/test_csv_export.py -v
```

Expected output:
```
tests/test_csv_export.py::test_csv_export_status_200                PASSED
tests/test_csv_export.py::test_csv_export_content_type              PASSED
tests/test_csv_export.py::test_csv_export_content_disposition       PASSED
tests/test_csv_export.py::test_csv_export_has_header_row            PASSED
tests/test_csv_export.py::test_csv_export_row_count                 PASSED
tests/test_csv_export.py::test_csv_export_row_values                PASSED
tests/test_csv_export.py::test_csv_export_null_fields_become_empty_string PASSED

7 passed
```

- [ ] **Step 4: Commit the implementation**

```bash
git add id3_organiser.py
git commit -m "feat: add GET /api/export/csv route"
```

---

### Task 3: Add the Export CSV button to the web UI

**Files:**
- Modify: `templates/index.html` (action bar, around line 269–278)

- [ ] **Step 1: Add the button**

In `templates/index.html`, find the action bar section (around line 269):

```html
<div class="action-bar">
  <input type="checkbox" id="selectAll" class="form-check-input me-1" title="Select all visible" />
  <button class="btn btn-sm btn-outline-success" onclick="moveSelected()">
    <i class="bi bi-arrow-right-circle me-1"></i>Move Selected
  </button>
  <button class="btn btn-sm" style="background:#7c3aed;color:#fff;border:none" onclick="moveAll()">
    <i class="bi bi-send-fill me-1"></i>Move All Pending
  </button>
  <span class="selected-count" id="selCount"></span>
</div>
```

Replace it with:

```html
<div class="action-bar">
  <input type="checkbox" id="selectAll" class="form-check-input me-1" title="Select all visible" />
  <button class="btn btn-sm btn-outline-success" onclick="moveSelected()">
    <i class="bi bi-arrow-right-circle me-1"></i>Move Selected
  </button>
  <button class="btn btn-sm" style="background:#7c3aed;color:#fff;border:none" onclick="moveAll()">
    <i class="bi bi-send-fill me-1"></i>Move All Pending
  </button>
  <button class="btn btn-sm btn-outline-info ms-auto" onclick="window.location='/api/export/csv'">
    <i class="bi bi-download me-1"></i>Export CSV
  </button>
  <span class="selected-count" id="selCount"></span>
</div>
```

- [ ] **Step 2: Manual smoke test**

Start the app against a music directory:

```bash
python id3_organiser.py /path/to/music
```

Open the browser, click "Export CSV" in the action bar. Verify:
- Browser prompts to save `music_library.csv`
- Opened in a spreadsheet: first row is headers, subsequent rows have track data, NULL fields are blank

- [ ] **Step 3: Commit**

```bash
git add templates/index.html
git commit -m "feat: add Export CSV button to web UI action bar"
```
