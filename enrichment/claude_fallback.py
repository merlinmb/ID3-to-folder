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
