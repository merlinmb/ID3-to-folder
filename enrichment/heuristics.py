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
    Only fills null fields. Requires >= 3 files per folder."""
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
