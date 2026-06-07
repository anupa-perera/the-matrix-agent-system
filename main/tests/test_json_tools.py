import pytest

from thematrix.prompts.json_tools import extract_json_object


def test_extract_json_object_reads_prefaced_fenced_json() -> None:
    payload = extract_json_object('Here is the JSON:\n```json\n{"ok": true}\n```')

    assert payload == {"ok": True}


def test_extract_json_object_reads_first_balanced_object() -> None:
    payload = extract_json_object('{"first": true}{"second": true}')

    assert payload == {"first": True}


def test_extract_json_object_skips_invalid_brace_before_valid_object() -> None:
    payload = extract_json_object('bad {"first": } then {"second": true}')

    assert payload == {"second": True}


def test_extract_json_object_rejects_non_object_json() -> None:
    with pytest.raises(ValueError, match="Expected a JSON object"):
        extract_json_object("[1, 2, 3]")
