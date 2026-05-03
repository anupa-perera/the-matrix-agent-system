from typer.testing import CliRunner

from thematrix import cli
from thematrix.schemas import AuthMode, FileChangeConsent, ProviderConfig


def test_start_opens_guided_setup_when_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("THE_MATRIX_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("THE_MATRIX_VAULT", str(tmp_path / "vault"))

    def fake_setup_ui(paths, vault, store, **kwargs) -> str:
        store.configure_provider(
            ProviderConfig(
                provider_id="ollama",
                selected_model="llama3.2",
                auth_mode=AuthMode.NONE,
                base_url="http://localhost:11434/v1",
                file_change_consent=FileChangeConsent.ASK_EACH_TIME,
            )
        )
        store.set_preference("onboarding_complete", True)
        return "http://127.0.0.1:1234/?token=test"

    opened: list[str] = []
    served_app: list[str] = []
    monkeypatch.setattr(cli, "serve_setup_ui", fake_setup_ui)
    monkeypatch.setattr(cli, "serve_app_ui", lambda *args, **kwargs: served_app.append("app") or "url")
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))

    result = CliRunner().invoke(
        cli.app,
        ["start", "--no-open", "--no-test-provider"],
    )

    assert result.exit_code == 0
    assert "Opening the guided local setup page" in result.output
    assert "The Matrix is ready." in result.output
    assert served_app == ["app"]
    assert opened == []


def test_start_uses_ready_setup_without_opening_setup(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("THE_MATRIX_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("THE_MATRIX_VAULT", str(tmp_path / "vault"))
    vault, store = cli.bootstrap(cli.MatrixPaths())
    store.configure_provider(
        ProviderConfig(
            provider_id="ollama",
            selected_model="llama3.2",
            auth_mode=AuthMode.NONE,
            base_url="http://localhost:11434/v1",
            file_change_consent=FileChangeConsent.ASK_EACH_TIME,
        )
    )
    store.set_preference("onboarding_complete", True)
    store.record_provider_verification(
        provider_id="ollama",
        ok=True,
        message="ready",
        model="llama3.2",
    )
    vault.append_log("Test setup", "Ready path")

    def fail_setup_ui(*args, **kwargs) -> str:
        raise AssertionError("setup UI should not open when setup is complete")

    monkeypatch.setattr(cli, "serve_setup_ui", fail_setup_ui)
    served_app: list[str] = []
    monkeypatch.setattr(cli, "serve_app_ui", lambda *args, **kwargs: served_app.append("app") or "url")

    result = CliRunner().invoke(
        cli.app,
        ["start", "--no-open"],
    )

    assert result.exit_code == 0
    assert "Provider already verified: ollama" in result.output
    assert "The Matrix is ready." in result.output
    assert served_app == ["app"]
