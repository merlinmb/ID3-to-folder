# enrichment/musicbrainz.py
import logging
import time
from difflib import SequenceMatcher

import musicbrainzngs

musicbrainzngs.set_useragent("ID3MusicOrganiser", "1.0", "id3organiser@local")

logger = logging.getLogger(__name__)


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
    Retries up to `retries` times with exponential backoff on NetworkError.
    Returns None on ResponseError or when no results found.
    """
    kwargs: dict = {"limit": 5}
    if title:
        kwargs["recording"] = title
    if artist:
        kwargs["artist"] = artist
    if album:
        kwargs["release"] = album

    logger.info("MusicBrainz query: artist=%r album=%r title=%r", artist, album, title)
    for attempt in range(retries):
        try:
            result = musicbrainzngs.search_recordings(**kwargs)
            break
        except musicbrainzngs.ResponseError as exc:
            logger.warning("MusicBrainz ResponseError: %s", exc)
            return None
        except musicbrainzngs.NetworkError as exc:
            if attempt < retries - 1:
                logger.warning("MusicBrainz NetworkError (attempt %d/%d): %s — retrying", attempt + 1, retries, exc)
                time.sleep(2 ** attempt)
            else:
                logger.error("MusicBrainz NetworkError: all %d attempts exhausted", retries)
                return None

    recordings = result.get("recording-list", [])
    if not recordings:
        logger.info("MusicBrainz: no recordings found for artist=%r title=%r", artist, title)
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
