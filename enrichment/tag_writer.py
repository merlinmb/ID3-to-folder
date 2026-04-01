# enrichment/tag_writer.py
from mutagen import File as MutagenFile
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3NoHeaderError, ID3FileType


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
    """Write tag fields to an audio file using mutagen.

    Only writes keys present in tags dict with non-None values.
    Raises TagWriteError if the file cannot be opened or written.
    """
    to_write = {
        _EASY_FIELD_MAP[field]: [str(value)]
        for field, value in tags.items()
        if _EASY_FIELD_MAP.get(field) and value is not None
    }
    if not to_write:
        return

    # Probe the file type without easy=True so we get the real class.
    try:
        probe = MutagenFile(file_path)
    except Exception as exc:
        raise TagWriteError(f"Cannot open {file_path}: {exc}") from exc

    if probe is None:
        raise TagWriteError(f"Unsupported format or corrupt file: {file_path}")

    if isinstance(probe, ID3FileType):
        # ID3-based files (MP3, etc.): use EasyID3 directly.
        # mutagen.File(easy=True) sometimes returns a raw ID3-tagged object
        # that rejects plain-string assignments, so we avoid that path entirely.
        try:
            easy = EasyID3(file_path)
        except ID3NoHeaderError:
            easy = EasyID3()
            easy.filename = file_path
        for key, val in to_write.items():
            easy[key] = val
        try:
            easy.save(file_path)
        except Exception as exc:
            raise TagWriteError(f"Cannot save {file_path}: {exc}") from exc
    else:
        # Non-ID3 formats (FLAC, OGG, M4A/EasyMP4, etc.) accept string values
        # through the easy=True interface or natively.
        try:
            audio = MutagenFile(file_path, easy=True)
        except Exception as exc:
            raise TagWriteError(f"Cannot open {file_path}: {exc}") from exc
        if audio is None:
            raise TagWriteError(f"Unsupported format or corrupt file: {file_path}")
        for key, val in to_write.items():
            audio[key] = val
        try:
            audio.save()
        except Exception as exc:
            raise TagWriteError(f"Cannot save {file_path}: {exc}") from exc
