from __future__ import annotations

import json
from typing import Any


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract the first JSON object from a model response."""

    cleaned = _strip_fences(text.strip())
    decoder = json.JSONDecoder()
    try:
        parsed = decoder.decode(cleaned)
    except json.JSONDecodeError:
        parsed = _decode_first_object(cleaned, decoder)
    if not isinstance(parsed, dict):
        raise ValueError("Expected a JSON object.")
    return parsed


def _decode_first_object(text: str, decoder: json.JSONDecoder) -> dict[str, Any]:
    first_error: json.JSONDecodeError | None = None
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError as exc:
            if first_error is None:
                first_error = exc
            continue
        if not isinstance(parsed, dict):
            raise ValueError("Expected a JSON object.")
        return parsed
    if first_error is not None:
        raise first_error
    raise ValueError("Expected a JSON object.")


def _strip_fences(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()
