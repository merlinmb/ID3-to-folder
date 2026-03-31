# tests/test_claude_fallback.py
import json
from unittest.mock import patch, MagicMock
import pytest
from enrichment.claude_fallback import query_claude


def _mock_response(payload: dict):
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps(payload))]
    return msg


def test_returns_suggestion_with_low_confidence():
    payload = {"artist": "Pink Floyd", "album": "Animals", "title": "Dogs",
               "year": "1977", "confidence": "high"}
    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.return_value = _mock_response(payload)
        result = query_claude("Dogs.mp3", "/music/Pink Floyd/Animals",
                              {}, ["Pigs.mp3", "Sheep.mp3"])
    # Claude confidence is always downgraded to 'low'
    assert result["confidence"] == "low"
    assert result["artist"] == "Pink Floyd"


def test_returns_none_on_api_error():
    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.side_effect = Exception("API error")
        result = query_claude("track.mp3", "/music/Unknown", {}, [])
    assert result is None


def test_returns_none_on_invalid_json():
    with patch("anthropic.Anthropic") as MockClient:
        bad = MagicMock()
        bad.content = [MagicMock(text="not json at all")]
        MockClient.return_value.messages.create.return_value = bad
        result = query_claude("track.mp3", "/music/Unknown", {}, [])
    assert result is None


def test_prompt_includes_neighbours():
    payload = {"artist": "A", "album": "B", "title": "C", "year": "2000", "confidence": "low"}
    captured_prompt = []

    def capture(*args, **kwargs):
        captured_prompt.append(kwargs.get("messages", [{}])[0].get("content", ""))
        return _mock_response(payload)

    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.side_effect = capture
        query_claude("track.mp3", "/music/Artist", {}, ["neighbour1.mp3", "neighbour2.mp3"])

    assert "neighbour1.mp3" in captured_prompt[0]


def test_submit_claude_batch_returns_batch_id():
    from enrichment.claude_fallback import submit_claude_batch
    mock_batch = MagicMock()
    mock_batch.id = "batch_abc123"
    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.batches.create.return_value = mock_batch
        batch_id = submit_claude_batch([{
            "track_id": 1, "filename": "Dogs.mp3",
            "folder_path": "/music/Pink Floyd/Animals",
            "partial_tags": {}, "neighbours": ["Pigs.mp3"],
        }])
    assert batch_id == "batch_abc123"


def test_collect_claude_batch_returns_none_when_processing():
    from enrichment.claude_fallback import collect_claude_batch
    mock_batch = MagicMock()
    mock_batch.processing_status = "in_progress"
    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.batches.retrieve.return_value = mock_batch
        result = collect_claude_batch("batch_abc123")
    assert result is None


def test_collect_claude_batch_returns_results_when_ended():
    from enrichment.claude_fallback import collect_claude_batch
    import json as _json
    mock_batch = MagicMock()
    mock_batch.processing_status = "ended"
    mock_result = MagicMock()
    mock_result.custom_id = "42"
    mock_result.result.type = "succeeded"
    mock_result.result.message.content = [
        MagicMock(text=_json.dumps({
            "artist": "Pink Floyd", "album": "Animals",
            "title": "Dogs", "year": "1977", "confidence": "high"
        }))
    ]
    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.batches.retrieve.return_value = mock_batch
        MockClient.return_value.messages.batches.results.return_value = [mock_result]
        results = collect_claude_batch("batch_abc123")
    assert results is not None
    assert len(results) == 1
    assert results[0]["track_id"] == 42
    assert results[0]["suggestion"]["artist"] == "Pink Floyd"
    assert results[0]["suggestion"]["confidence"] == "low"


def test_collect_claude_batch_skips_errored_results():
    from enrichment.claude_fallback import collect_claude_batch
    mock_batch = MagicMock()
    mock_batch.processing_status = "ended"
    mock_result = MagicMock()
    mock_result.custom_id = "99"
    mock_result.result.type = "errored"
    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.batches.retrieve.return_value = mock_batch
        MockClient.return_value.messages.batches.results.return_value = [mock_result]
        results = collect_claude_batch("batch_abc123")
    assert results == []
