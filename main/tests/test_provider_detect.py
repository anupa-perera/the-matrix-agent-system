from thematrix.providers import detect


def test_detect_ollama_reports_models(monkeypatch) -> None:
    monkeypatch.setattr(
        detect,
        "_get_json",
        lambda url, timeout: {"models": [{"name": "llama3.2"}]},
    )

    result = detect._detect_ollama(timeout_seconds=0.001)

    assert result.provider_id == "ollama"
    assert result.reachable is True
    assert result.models == ["llama3.2"]


def test_detect_lm_studio_reports_models(monkeypatch) -> None:
    monkeypatch.setattr(
        detect,
        "_get_json",
        lambda url, timeout: {"data": [{"id": "local-model"}]},
    )

    result = detect._detect_lm_studio(timeout_seconds=0.001)

    assert result.provider_id == "lmstudio"
    assert result.reachable is True
    assert result.models == ["local-model"]
