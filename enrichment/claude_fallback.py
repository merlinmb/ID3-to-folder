# enrichment/claude_fallback.py
import json
import logging

import anthropic

logger = logging.getLogger(__name__)


def _build_prompt(filename: str, folder_path: str, partial_tags: dict, neighbours: list[str]) -> str:
    """Build the prompt string for Claude metadata identification."""
    neighbour_str = ", ".join(neighbours[:10]) if neighbours else "none"
    partial_str = json.dumps(
        {k: v for k, v in partial_tags.items() if v}, indent=None
    )

    return (
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
    prompt = _build_prompt(filename, folder_path, partial_tags, neighbours)

    logger.info("Claude query (single): filename=%r folder=%r", filename, folder_path)
    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        data = json.loads(response.content[0].text)
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        logger.warning("Claude response parse error for %r: %s", filename, exc)
        return None
    except Exception as exc:
        logger.error("Claude API error for %r: %s", filename, exc)
        return None

    # Always treat as low confidence — all Claude results go to review queue
    data["confidence"] = "low"
    logger.info("Claude result: filename=%r artist=%r album=%r title=%r", filename, data.get("artist"), data.get("album"), data.get("title"))
    return data


def submit_claude_batch(track_prompts: list[dict]) -> str:
    """Submit a batch of track metadata requests to Anthropic Message Batches API.

    Each element of track_prompts must have:
        track_id: int, filename: str, folder_path: str,
        partial_tags: dict, neighbours: list[str]

    Returns the Anthropic batch ID string. Raises on API error.
    """
    client = anthropic.Anthropic()
    requests = [
        {
            "custom_id": str(tp["track_id"]),
            "params": {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 256,
                "messages": [{
                    "role": "user",
                    "content": _build_prompt(
                        tp["filename"], tp["folder_path"],
                        tp["partial_tags"], tp["neighbours"]
                    ),
                }],
            },
        }
        for tp in track_prompts
    ]
    logger.info("Submitting Claude batch: %d request(s)", len(requests))
    batch = client.messages.batches.create(requests=requests)
    logger.info("Claude batch created: id=%s", batch.id)
    return batch.id


def collect_claude_batch(batch_id: str) -> list[dict] | None:
    """Poll a previously submitted batch.

    Returns None if still processing.
    Returns list of {track_id: int, suggestion: dict} when ended.
    suggestion always has confidence='low'. Skips errored results.
    """
    client = anthropic.Anthropic()
    batch = client.messages.batches.retrieve(batch_id)
    logger.info("Claude batch %s status: %s", batch_id, batch.processing_status)
    if batch.processing_status != "ended":
        return None

    results = []
    errors = 0
    for result in client.messages.batches.results(batch_id):
        if result.result.type != "succeeded":
            logger.warning("Claude batch %s: item %s failed with type=%s", batch_id, result.custom_id, result.result.type)
            errors += 1
            continue
        try:
            data = json.loads(result.result.message.content[0].text)
            data["confidence"] = "low"
            results.append({"track_id": int(result.custom_id), "suggestion": data})
        except (json.JSONDecodeError, KeyError, IndexError, ValueError) as exc:
            logger.warning("Claude batch %s: parse error for item %s: %s", batch_id, result.custom_id, exc)
            errors += 1
            continue
    logger.info("Claude batch %s collected: %d succeeded, %d failed", batch_id, len(results), errors)
    return results
