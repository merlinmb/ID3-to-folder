# CSV Export Design

**Date:** 2026-03-31  
**Feature:** Export all scanned tracks to CSV from the web UI

---

## Overview

Add a "Export CSV" button to the web UI action bar. Clicking it downloads a CSV file containing all tracks currently in the database with their music metadata fields.

---

## Backend

**New route:** `GET /api/export/csv`

- Queries all rows from the `tracks` table, ordered by `artist NULLS LAST, album NULLS LAST, track_number, title`
- Writes rows using Python's stdlib `csv.writer` into an `io.StringIO` buffer
- Returns the buffer content as a Flask `Response` with:
  - `mimetype="text/csv"`
  - `Content-Disposition: attachment; filename="music_library.csv"`

**CSV columns (in order):**

| Column | DB field |
|--------|----------|
| Filename | `filename` |
| Artist | `artist` |
| Album | `album` |
| Track Number | `track_number` |
| Title | `title` |
| Genre | `genre` |
| Year | `year` |
| Duration | `duration` |
| Original Path | `original_path` |
| Destination Path | `destination_path` |

No filtering is applied — all tracks are always exported.

---

## Frontend

Add an "Export CSV" button to the action bar in `templates/index.html`, alongside the existing Move buttons.

```html
<button class="btn btn-sm btn-outline-info" onclick="window.location='/api/export/csv'">
  <i class="bi bi-download me-1"></i>Export CSV
</button>
```

`window.location` assignment triggers a native browser file download with no JS fetch logic required.

---

## Constraints

- No new dependencies: uses Python stdlib `csv` and `io` modules
- No filtering by current UI filters — always exports all tracks
- File is named `music_library.csv`
