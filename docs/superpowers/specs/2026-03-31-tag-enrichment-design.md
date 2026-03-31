# Tag Enrichment Design

**Date:** 2026-03-31
**Status:** Approved

## Overview

A pipeline for automatically filling in missing ID3 tag information for ~3,000 unmatched tracks. Uses a three-stage approach: offline heuristics → MusicBrainz API (daily background thread) → Claude AI fallback. High-confidence results auto-apply; low-confidence results go to a review queue in the web UI. Approved tags are written back to the audio files via mutagen and destination paths are recalculated.

---

## Data Model

### New table: `enrichment_candidates`

One row per track per enrichment attempt.

```sql
CREATE TABLE enrichment_candidates (
    id                INTEGER  PRIMARY KEY AUTOINCREMENT,
    track_id          INTEGER  NOT NULL REFERENCES tracks(id),
    source            TEXT     NOT NULL,  -- 'heuristic' | 'musicbrainz' | 'claude'
    suggested_artist  TEXT,
    suggested_album   TEXT,
    suggested_title   TEXT,
    suggested_year    TEXT,
    suggested_genre   TEXT,
    confidence        TEXT     NOT NULL,  -- 'high' | 'low'
    status            TEXT     NOT NULL DEFAULT 'pending_review',
                                          -- 'pending_review' | 'approved' | 'rejected' | 'applied'
    raw_response      TEXT,               -- JSON blob from API/AI
    created_at        TEXT     NOT NULL
);
```

### New table: `enrichment_runs`

Tracks daily batch state for resumability.

```sql
CREATE TABLE enrichment_runs (
    id                      INTEGER  PRIMARY KEY AUTOINCREMENT,
    started_at              TEXT     NOT NULL,
    last_processed_track_id INTEGER,
    tracks_processed        INTEGER  DEFAULT 0,
    tracks_remaining        INTEGER,
    daily_limit             INTEGER  NOT NULL,
    status                  TEXT     NOT NULL DEFAULT 'running'
                                     -- 'running' | 'paused' | 'complete'
);
```

### Changes to existing `tracks` table

Two new columns:

| Column | Type | Description |
|---|---|---|
| `enrichment_status` | TEXT | `none` \| `in_progress` \| `enriched` \| `review_needed` |
| `tags_written_at` | TEXT | ISO timestamp when mutagen last wrote tags to the file |

---

## Enrichment Pipeline

Invoked via: `python id3_organiser.py enrich [OPTIONS]`

### Stage 1 — Heuristics (one-shot, offline)

Runs immediately on first invocation across all unmatched tracks. Fast and free — no rate limiting needed.

**Signals extracted from `original_path`:**
- Folder depth pattern `…/Artist/Album/track.mp3` → artist + album
- Filename patterns: `01 - Title.mp3`, `Artist - Title.mp3`, `01. Title.mp3`
- Same-folder grouping: if ≥3 files share a folder and at least one has valid tags, propagate artist/album to siblings with missing tags

**Confidence scoring:**
- `high` — Artist + Album both extracted from path structure
- `low` — only partial information available

High-confidence heuristic results are auto-applied (skip review queue) — tags are written to the file and destination path is recalculated immediately. Low-confidence go to `pending_review`.

Same-folder propagation only fills **null** fields; it never overwrites a tag that already has a value.

### Stage 2 — MusicBrainz API (daily background thread)

A long-lived `enrich` process starts a scheduler thread that wakes daily to process the next batch. Uses the `musicbrainz_ngs` Python library (free, no API key required).

**Daily thread behaviour:**
- On startup: runs Stage 1 (heuristics) immediately, then schedules Stage 2
- Each daily tick: processes up to `--daily-limit` tracks (default: 500) that are still unmatched after Stage 1
- Queries built from available partial info: heuristic artist/album + filename
- Confidence: exact title+artist match = `high`; fuzzy match = `low`
- Writes results to `enrichment_candidates`; updates `enrichment_runs` for resumability
- If process restarts: reads last `enrichment_runs` row to skip already-processed tracks and respect remaining daily quota

**CLI options:**
- `--daily-limit N` — max MusicBrainz lookups per day (default: 500)
- `--run-now` — skip the daily schedule and run the next batch immediately

### Stage 3 — Claude AI fallback (runs after Stage 2 in the same daily thread)

For tracks where MusicBrainz returned no useful result. Sends Claude a structured prompt containing:
- Filename
- Folder path
- Any partial tags already in the DB
- Filenames of neighbouring tracks in the same folder (context)

Claude returns structured JSON: `{ artist, album, title, year, confidence }`.

Confidence from Claude is always treated as `low` unless the response is explicit and unambiguous — all Claude results go to the review queue.

**Rate limiting:** Claude calls are batched conservatively within the daily thread (max 100/day by default, configurable via `--claude-limit`).

---

## Tag Write-back

Triggered after a user approves one or more candidates in the web UI.

1. POST to `/api/enrich/apply` with list of `enrichment_candidate` IDs
2. For each: mutagen writes corrected tags to the audio file on disk
3. `_calculate_destination` re-runs to produce a new `destination_path`
4. `tracks` row updated: `artist`, `album`, `title`, `year`, `genre`, `matched`, `destination_path`, `tags_written_at`, `enrichment_status = 'enriched'`
5. `enrichment_candidates` row updated: `status = 'applied'`
6. SSE stream (same pattern as `/api/move/stream`) reports progress to the UI

---

## Web UI Changes

A new **"Enrich"** tab added to the existing web interface.

### Review Queue

Paginated table of `pending_review` enrichment candidates:

| Column | Notes |
|---|---|
| Filename + original path | Read-only |
| Suggested artist / album / title / year | Editable inline before approving |
| Source badge | `heuristic` \| `musicbrainz` \| `claude` |
| Confidence badge | `high` \| `low` |
| Approve / Reject | Per-row and bulk-select |

### Enrichment Status Bar

Live state of the daily batch thread:
- Tracks remaining / processed today / total unmatched
- Next scheduled run countdown
- "Run Now" button → triggers immediate batch (calls `/api/enrich/run-now`)

### Batch Approval Flow

1. User reviews candidates, edits inline as needed
2. Selects one or more rows, clicks "Write Tags"
3. POST `/api/enrich/apply` → server writes tags + recalculates paths
4. SSE stream reports per-file progress
5. UI updates rows to `applied` state

### Track List Filter

Existing track list gains an `enrichment_status` filter option:
- All
- Not enriched (`none`)
- In progress
- Review needed
- Enriched

---

## New API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/enrich/candidates` | Paginated list of enrichment candidates (filterable by status, source, confidence) |
| POST | `/api/enrich/apply` | Write approved tags to files, recalculate paths |
| GET | `/api/enrich/apply/stream` | SSE stream for apply progress |
| GET | `/api/enrich/status` | Current batch thread state (for status bar) |
| POST | `/api/enrich/run-now` | Trigger immediate batch run |

---

## New Dependencies

| Package | Purpose |
|---|---|
| `musicbrainzngs` | MusicBrainz API client |
| `anthropic` | Claude API client (Stage 3 fallback) |
| `schedule` | Daily thread scheduling |

---

## Error Handling

- MusicBrainz rate limit (HTTP 503): exponential backoff, retry up to 3 times, then skip track for current day
- Claude API errors: log and skip; track marked `enrichment_status = 'none'` to retry next run
- Tag write failure (file locked, permissions): log error to `tracks.error_message`, leave `tags_written_at` null, surface in UI
- DB access: both `enrich` process and Flask app use SQLite WAL mode to avoid write contention

---

## Out of Scope

- Acoustic fingerprinting (AcoustID/Chromaprint) — not planned
- Album art fetching — not planned
- Automatic scheduling via cron/Task Scheduler — user sets this up externally using `--run-now`
