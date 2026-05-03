import json
import re
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from thematrix.config import MatrixPaths
from thematrix.memory import MemoryVault, RuntimeStore
from thematrix.providers import ProviderDetection
from thematrix.security import InMemorySecretStore, Keymaker
from thematrix.ui.setup_server import (
    MAX_SETUP_BODY_BYTES,
    apply_setup_form,
    render_setup_form,
    serve_setup_ui,
)


def test_setup_ui_applies_form_without_storing_raw_secret(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    vault = MemoryVault(paths.vault)
    store = RuntimeStore(paths.runtime_db)
    vault.initialize()
    store.initialize()
    keymaker = Keymaker(InMemorySecretStore())

    result = apply_setup_form(
        {
            "provider_id": "openrouter",
            "model": "openai/gpt-5-mini",
            "auth_mode": "api_key",
            "api_key": "sk-test",
            "privacy_mode": "ask_each_time",
            "file_change_consent": "ask_each_time",
            "guarded_shell_enabled": "on",
        },
        paths,
        vault,
        store,
        keymaker,
    )

    config = store.get_default_provider_config()
    assert result.ok
    assert config is not None
    assert config.provider_id == "openrouter"
    assert config.secret_ref == "keyring:provider:openrouter:api_key"
    assert keymaker.resolve_api_key(config.secret_ref) == "sk-test"
    assert store.get_preference("onboarding_complete") is True
    assert "sk-test" not in (paths.vault / "log.md").read_text(encoding="utf-8")


def test_setup_ui_rejects_oauth_until_flow_exists(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    vault = MemoryVault(paths.vault)
    store = RuntimeStore(paths.runtime_db)
    vault.initialize()
    store.initialize()

    result = apply_setup_form(
        {
            "provider_id": "openrouter",
            "model": "openai/gpt-5-mini",
            "auth_mode": "oauth",
            "privacy_mode": "ask_each_time",
            "file_change_consent": "ask_each_time",
        },
        paths,
        vault,
        store,
        Keymaker(InMemorySecretStore()),
    )

    assert not result.ok
    assert "OAuth setup is not wired yet" in result.message
    assert store.get_default_provider_config() is None


def test_setup_ui_form_contains_session_token() -> None:
    html = render_setup_form("token-123")

    assert "/save?token=token-123" in html
    assert "The Matrix Setup" in html
    assert "syncProvider()" in html
    assert 'id="auth_mode_row"' in html
    assert 'provider.auth_modes[0] === "none"' in html


def test_setup_ui_form_embeds_provider_defaults() -> None:
    html = render_setup_form(
        "token-123",
        detections=[
            ProviderDetection(
                provider_id="ollama",
                display_name="Ollama",
                reachable=True,
                base_url="http://localhost:11434/v1",
                models=["llama3.2:latest"],
                message="Ollama is reachable.",
            )
        ],
    )
    match = re.search(
        r'<script id="provider-data" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )

    assert match is not None
    providers = json.loads(match.group(1))
    ollama = next(provider for provider in providers if provider["provider_id"] == "ollama")
    openrouter = next(
        provider for provider in providers if provider["provider_id"] == "openrouter"
    )
    assert ollama["auth_modes"] == ["none"]
    assert ollama["default_base_url"] == "http://localhost:11434/v1"
    assert ollama["detected_reachable"] is True
    assert ollama["detected_models"] == ["llama3.2:latest"]
    assert openrouter["suggested_models"][0] == "openai/gpt-5-mini"


def test_setup_ui_server_binds_localhost_and_saves_form(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    vault = MemoryVault(paths.vault)
    store = RuntimeStore(paths.runtime_db)
    vault.initialize()
    store.initialize()
    captured_url: list[str] = []
    ready = Event()

    def run_server() -> None:
        serve_setup_ui(
            paths,
            vault,
            store,
            port=0,
            open_browser=False,
            keymaker_factory=lambda: Keymaker(InMemorySecretStore()),
            url_callback=lambda url: (captured_url.append(url), ready.set()),
            timeout_seconds=30,
        )

    thread = Thread(target=run_server, daemon=True)
    thread.start()
    assert ready.wait(timeout=5)
    url = captured_url[0]
    assert url.startswith("http://127.0.0.1:")

    parsed = urlparse(url)
    try:
        urlopen(f"http://{parsed.netloc}/", timeout=5)
    except HTTPError as exc:
        assert exc.code == 403
    else:
        raise AssertionError("Setup UI allowed a request without the session token.")

    payload = urlencode(
        {
            "provider_id": "ollama",
            "model": "llama3.2",
            "auth_mode": "none",
            "privacy_mode": "local_only",
            "file_change_consent": "ask_each_time",
            "guarded_shell_enabled": "on",
        }
    ).encode("utf-8")
    request = Request(
        f"http://{parsed.netloc}/save?{parsed.query}",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        body = response.read().decode("utf-8")

    thread.join(timeout=5)
    config = store.get_default_provider_config()
    assert "Setup saved" in body
    assert config is not None
    assert config.provider_id == "ollama"
    assert store.get_preference("default_privacy_mode") == "local_only"


def test_setup_ui_rejects_oversized_body(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    vault = MemoryVault(paths.vault)
    store = RuntimeStore(paths.runtime_db)
    vault.initialize()
    store.initialize()
    captured_url: list[str] = []
    ready = Event()

    def run_server() -> None:
        serve_setup_ui(
            paths,
            vault,
            store,
            port=0,
            open_browser=False,
            keymaker_factory=lambda: Keymaker(InMemorySecretStore()),
            url_callback=lambda url: (captured_url.append(url), ready.set()),
            timeout_seconds=30,
        )

    thread = Thread(target=run_server, daemon=True)
    thread.start()
    assert ready.wait(timeout=5)
    parsed = urlparse(captured_url[0])
    request = Request(
        f"http://{parsed.netloc}/save?{parsed.query}",
        data=b"x" * (MAX_SETUP_BODY_BYTES + 1),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        urlopen(request, timeout=5)
    except HTTPError as exc:
        assert exc.code == 413
    else:
        raise AssertionError("Setup UI accepted an oversized request body.")

    payload = urlencode(
        {
            "provider_id": "ollama",
            "model": "llama3.2",
            "auth_mode": "none",
            "privacy_mode": "local_only",
            "file_change_consent": "ask_each_time",
        }
    ).encode("utf-8")
    with urlopen(
        Request(
            f"http://{parsed.netloc}/save?{parsed.query}",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        ),
        timeout=5,
    ):
        pass
    thread.join(timeout=5)


def test_setup_ui_rejects_malformed_content_length(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    vault = MemoryVault(paths.vault)
    store = RuntimeStore(paths.runtime_db)
    vault.initialize()
    store.initialize()
    captured_url: list[str] = []
    ready = Event()

    def run_server() -> None:
        serve_setup_ui(
            paths,
            vault,
            store,
            port=0,
            open_browser=False,
            keymaker_factory=lambda: Keymaker(InMemorySecretStore()),
            url_callback=lambda url: (captured_url.append(url), ready.set()),
            timeout_seconds=30,
        )

    thread = Thread(target=run_server, daemon=True)
    thread.start()
    assert ready.wait(timeout=5)
    parsed = urlparse(captured_url[0])

    import http.client

    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    conn.putrequest("POST", f"/save?{parsed.query}")
    conn.putheader("Content-Length", "not-a-number")
    conn.endheaders()
    response = conn.getresponse()
    assert response.status == 400
    conn.close()

    payload = urlencode(
        {
            "provider_id": "ollama",
            "model": "llama3.2",
            "auth_mode": "none",
            "privacy_mode": "local_only",
            "file_change_consent": "ask_each_time",
        }
    ).encode("utf-8")
    with urlopen(
        Request(
            f"http://{parsed.netloc}/save?{parsed.query}",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        ),
        timeout=5,
    ):
        pass
    thread.join(timeout=5)
