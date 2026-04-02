# enrichment/tag_writer.py
import logging
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3NoHeaderError

logger = logging.getLogger(__name__)


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
    ext = Path(file_path).suffix.lower()
    try:
        if ext == ".mp3":
            # MutagenFile(easy=True) can return a raw ID3 object for MP3s
            # without an existing ID3v2 header, causing TypeError on assignment.
            # Use EasyID3 directly so string assignment always works.
            try:
                audio = EasyID3(file_path)
            except ID3NoHeaderError:
                audio = EasyID3()
        else:
            audio = MutagenFile(file_path, easy=True)
            if audio is None:
                raise TagWriteError(f"Unsupported format or corrupt file: {file_path}")
            if audio.tags is None:
                audio.add_tags()
    except TagWriteError:
        raise
    except Exception as exc:
        raise TagWriteError(f"Cannot open {file_path}: {exc}") from exc

    written = {}
    for field, value in tags.items():
        easy_key = _EASY_FIELD_MAP.get(field)
        if easy_key and value is not None:
            audio[easy_key] = [str(value)]
            written[field] = value

    try:
        audio.save(file_path)
        logger.info("Tags written: %s fields=%s", Path(file_path).name, written)
    except Exception as exc:
        raise TagWriteError(f"Cannot save {file_path}: {exc}") from exc
