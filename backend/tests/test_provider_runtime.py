from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.provider_runtime import (
    ProviderResponseError,
    RuntimeCredential,
    _openai_choice_text,
    _openai_compatible,
    _openai_request_body,
    extract_json_object,
)


def test_extract_json_object_reads_fenced_and_rejects_empty_prose() -> None:
    value = extract_json_object('```json\n{"name": "Airport communication agent"}\n```')
    assert value == {"name": "Airport communication agent"}
    with pytest.raises(HTTPException) as exc:
        extract_json_object("I can design an airport communication agent for you.")
    assert exc.value.status_code == 502
    assert exc.value.detail == "Model did not return a JSON object"


def test_openai_choice_text_reads_parts_and_rejects_empty_reasoning_only() -> None:
    assert (
        _openai_choice_text(
            {"choices": [{"message": {"content": [{"type": "text", "text": '{"ok": true}'}]}}]}
        )
        == '{"ok": true}'
    )
    with pytest.raises(KeyError):
        _openai_choice_text({"choices": [{"message": {"content": "", "reasoning": "thinking"}}]})


def test_groq_json_payload_uses_completion_tokens_and_low_reasoning() -> None:
    credential = RuntimeCredential("platform", "llm", "groq", "gsk_test", {})
    body = _openai_request_body(
        credential,
        "openai/gpt-oss-120b",
        [{"role": "user", "content": "design"}],
        max_tokens=4096,
        json_object=True,
    )
    assert body["max_completion_tokens"] == 4096
    assert "max_tokens" not in body
    assert body["reasoning_effort"] == "low"
    assert body["include_reasoning"] is False
    assert body["response_format"] == {"type": "json_object"}


def test_openai_compatible_treats_empty_content_as_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "choices": [{"message": {"content": "", "reasoning": "spent the budget thinking"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 1200},
    }
    monkeypatch.setattr("app.provider_runtime.httpx.post", lambda *args, **kwargs: response)
    credential = RuntimeCredential("platform", "llm", "groq", "gsk_test", {})
    with pytest.raises(ProviderResponseError) as exc:
        _openai_compatible(
            credential,
            "openai/gpt-oss-120b",
            [{"role": "user", "content": "design"}],
            max_tokens=4096,
            json_object=True,
        )
    assert exc.value.cost_uncertain is True
