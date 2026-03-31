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
