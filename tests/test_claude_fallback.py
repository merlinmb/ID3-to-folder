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
