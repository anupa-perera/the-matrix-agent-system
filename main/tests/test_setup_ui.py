import json
import re
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from thematrix.config import MatrixPaths
from thematrix.memory import MemoryVault, RuntimeStore
from thematrix.providers import ProviderDetection
from thematrix.schemas import AuthMode, ProviderConfig, ReasoningEffort
from thematrix.security import InMemorySecretStore, Keymaker
from thematrix.ui.setup_server import (
    MAX_SETUP_BODY_BYTES,
    _message_page as _setup_message_page,
    apply_setup_form,
    render_setup_form,
    serve_setup_ui,
)


def _assert_matrix_background(html: str) -> None:
    assert 'id="matrix-rain"' in html
    assert "getElementById('matrix-rain')" in html or 'getElementById("matrix-rain")' in html


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


def test_setup_ui_applies_openrouter_oauth_without_storing_raw_secret(tmp_path) -> None:
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
            "auth_mode": "oauth",
            "oauth_api_key": "sk-oauth-test",
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
    assert config.auth_mode == AuthMode.OAUTH
    assert config.secret_ref == "keyring:provider:openrouter:api_key"
    assert keymaker.resolve_api_key(config.secret_ref) == "sk-oauth-test"
    assert "sk-oauth-test" not in (paths.vault / "log.md").read_text(encoding="utf-8")


def test_setup_ui_applies_codex_external_auth_without_secret(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    vault = MemoryVault(paths.vault)
    store = RuntimeStore(paths.runtime_db)
    vault.initialize()
    store.initialize()

    result = apply_setup_form(
        {
            "provider_id": "openai-codex",
            "model": "gpt-5.5",
            "reasoning_effort": "high",
            "auth_mode": "external",
            "privacy_mode": "ask_each_time",
            "file_change_consent": "ask_each_time",
            "guarded_shell_enabled": "on",
        },
        paths,
        vault,
        store,
        Keymaker(InMemorySecretStore()),
    )

    config = store.get_default_provider_config()
    assert result.ok
    assert config is not None
    assert config.provider_id == "openai-codex"
    assert config.selected_model == "gpt-5.5"
    assert config.reasoning_effort == ReasoningEffort.HIGH
    assert config.auth_mode == AuthMode.EXTERNAL
    assert config.secret_ref is None
    assert store.get_preference("onboarding_complete") is True


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
    assert "Use Sign in with OpenRouter" in result.message
    assert store.get_default_provider_config() is None

    result = apply_setup_form(
        {
            "provider_id": "gemini",
            "model": "gemini-2.5-pro",
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
    assert "OAuth setup for Gemini is not automated yet" in result.message
    assert store.get_default_provider_config() is None


def test_setup_ui_form_contains_session_token() -> None:
    html = render_setup_form("token-123")

    _assert_matrix_background(html)
    assert "/save?token=token-123" in html
    assert "The Matrix Setup" in html
    assert "Start here" in html
    assert "Start The Matrix" in html
    assert "Advanced settings" in html
    assert "Model choice" in html
    assert "Reasoning effort" in html
    assert "syncProvider()" in html
    assert 'list="models"' not in html
    assert "<datalist" not in html
    assert 'id="auth_mode_row"' in html
    assert "No sign-in needed" in html
    assert "Official app sign-in" in html
    assert "Back to Dashboard" not in html
    assert 'provider.oauth_automated' in html
    assert "Sign in with OpenRouter" in html
    assert 'authModes.length <= 1' in html
    assert '<details class="notes provider-registry">' in html
    assert "<summary>Provider Registry</summary>" in html

    settings_html = render_setup_form("token-123", dashboard_url="/dashboard?token=token-123")
    _assert_matrix_background(settings_html)
    assert "Back to Dashboard" in settings_html
    assert "/dashboard?token=token-123" in settings_html


def test_setup_message_pages_include_matrix_background() -> None:
    html = _setup_message_page("Forbidden", "Invalid token.")

    _assert_matrix_background(html)


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
    codex = next(provider for provider in providers if provider["provider_id"] == "openai-codex")
    assert ollama["auth_modes"] == ["none"]
    assert ollama["default_base_url"] == "http://localhost:11434/v1"
    assert ollama["detected_reachable"] is True
    assert ollama["detected_models"] == ["llama3.2:latest"]
    assert codex["auth_modes"] == ["external"]
    assert "gpt-5.5" in codex["suggested_models"]
    assert codex["supported_reasoning_efforts"] == ["low", "medium", "high", "xhigh"]
    assert codex["oauth_automated"] is False
    assert openrouter["suggested_models"][0] == "openai/gpt-5-mini"
    assert openrouter["oauth_automated"] is True
    assert openrouter["oauth_label"] == "Sign in with OpenRouter"


def test_setup_ui_prefills_current_provider_config() -> None:
    current = ProviderConfig(
        provider_id="openai-codex",
        selected_model="gpt-5.5",
        auth_mode=AuthMode.EXTERNAL,
        reasoning_effort=ReasoningEffort.HIGH,
    )

    html = render_setup_form("token-123", current_config=current)
    match = re.search(
        r'<script id="current-config" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )

    assert match is not None
    payload = json.loads(match.group(1))
    assert payload == {
        "provider_id": "openai-codex",
        "selected_model": "gpt-5.5",
        "reasoning_effort": "high",
    }


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

    config = store.get_default_provider_config()
    assert "Setup saved" in body
    assert "Continuing to dashboard..." in body
    assert "Open Dashboard" in body
    assert f"/dashboard?{parsed.query}" in body
    assert paths.dashboard_file.exists()
    assert config is not None
    assert config.provider_id == "ollama"
    assert store.get_preference("default_privacy_mode") == "local_only"

    with urlopen(f"http://{parsed.netloc}/dashboard?{parsed.query}", timeout=5) as response:
        dashboard_body = response.read().decode("utf-8")

    thread.join(timeout=5)
    assert not thread.is_alive()
    assert "The Matrix Dashboard" in dashboard_body


def test_setup_ui_server_openrouter_oauth_callback_saves_form(tmp_path, monkeypatch) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    vault = MemoryVault(paths.vault)
    store = RuntimeStore(paths.runtime_db)
    vault.initialize()
    store.initialize()
    keymaker = Keymaker(InMemorySecretStore())
    captured_url: list[str] = []
    ready = Event()

    monkeypatch.setattr(
        "thematrix.ui.setup_server.exchange_openrouter_code",
        lambda code, verifier: "sk-oauth-callback",
    )

    def run_server() -> None:
        serve_setup_ui(
            paths,
            vault,
            store,
            port=0,
            open_browser=False,
            keymaker_factory=lambda: keymaker,
            url_callback=lambda url: (captured_url.append(url), ready.set()),
            timeout_seconds=30,
        )

    thread = Thread(target=run_server, daemon=True)
    thread.start()
    assert ready.wait(timeout=5)
    parsed = urlparse(captured_url[0])
    token = parse_qs(parsed.query)["token"][0]

    import http.client

    start_query = urlencode(
        {
            "token": token,
            "provider_id": "openrouter",
            "model": "openai/gpt-5-mini",
            "auth_mode": "oauth",
            "privacy_mode": "ask_each_time",
            "file_change_consent": "ask_each_time",
            "base_url": "https://openrouter.ai/api/v1",
        }
    )
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    conn.request("GET", f"/oauth/openrouter/start?{start_query}")
    response = conn.getresponse()
    assert response.status == 302
    location = response.getheader("Location")
    assert response.getheader("Referrer-Policy") == "no-referrer"
    response.read()
    conn.close()
    assert location is not None

    auth_query = parse_qs(urlparse(location).query)
    callback_url = auth_query["callback_url"][0]
    callback_query = parse_qs(urlparse(callback_url).query)
    flow_id = callback_query["flow"][0]
    state = callback_query["state"][0]
    assert "token" not in callback_query

    callback_query_text = urlencode({"flow": flow_id, "state": state, "code": "provider-code"})
    with urlopen(
        f"http://{parsed.netloc}/oauth/openrouter/callback?{callback_query_text}",
        timeout=5,
    ) as response:
        body = response.read().decode("utf-8")

    config = store.get_default_provider_config()
    assert "OAuth connected" in body
    assert "Open Dashboard" in body
    assert config is not None
    assert config.auth_mode == AuthMode.OAUTH
    assert config.provider_id == "openrouter"
    assert config.secret_ref is not None
    assert keymaker.resolve_api_key(config.secret_ref) == "sk-oauth-callback"
    assert "sk-oauth-callback" not in (paths.vault / "log.md").read_text(encoding="utf-8")

    with urlopen(f"http://{parsed.netloc}/dashboard?token={token}", timeout=5):
        pass
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_setup_ui_rejects_oauth_callback_without_valid_state(tmp_path, monkeypatch) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    vault = MemoryVault(paths.vault)
    store = RuntimeStore(paths.runtime_db)
    vault.initialize()
    store.initialize()
    keymaker = Keymaker(InMemorySecretStore())
    captured_url: list[str] = []
    ready = Event()

    monkeypatch.setattr(
        "thematrix.ui.setup_server.exchange_openrouter_code",
        lambda code, verifier: "sk-oauth-callback",
    )

    def run_server() -> None:
        serve_setup_ui(
            paths,
            vault,
            store,
            port=0,
            open_browser=False,
            keymaker_factory=lambda: keymaker,
            url_callback=lambda url: (captured_url.append(url), ready.set()),
            timeout_seconds=30,
        )

    thread = Thread(target=run_server, daemon=True)
    thread.start()
    assert ready.wait(timeout=5)
    parsed = urlparse(captured_url[0])
    token = parse_qs(parsed.query)["token"][0]

    import http.client

    start_query = urlencode(
        {
            "token": token,
            "provider_id": "openrouter",
            "model": "openai/gpt-5-mini",
            "auth_mode": "oauth",
            "privacy_mode": "ask_each_time",
            "file_change_consent": "ask_each_time",
            "base_url": "https://openrouter.ai/api/v1",
        }
    )
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    conn.request("GET", f"/oauth/openrouter/start?{start_query}")
    response = conn.getresponse()
    location = response.getheader("Location")
    set_cookie = response.getheader("Set-Cookie")
    response.read()
    conn.close()
    assert response.status == 302
    assert "HttpOnly" in set_cookie
    assert "SameSite=Strict" in set_cookie
    assert location is not None

    auth_query = parse_qs(urlparse(location).query)
    callback_query = parse_qs(urlparse(auth_query["callback_url"][0]).query)
    flow_id = callback_query["flow"][0]
    callback_query_text = urlencode({"flow": flow_id, "state": "wrong", "code": "provider-code"})
    try:
        urlopen(
            f"http://{parsed.netloc}/oauth/openrouter/callback?{callback_query_text}",
            timeout=5,
        )
    except HTTPError as exc:
        assert exc.code == 403
    else:
        raise AssertionError("OAuth callback accepted a forged state.")

    assert store.get_default_provider_config() is None

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
    with urlopen(f"http://{parsed.netloc}/dashboard?{parsed.query}", timeout=5):
        pass
    thread.join(timeout=5)
    assert not thread.is_alive()


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

    import http.client

    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    conn.putrequest("POST", f"/save?{parsed.query}")
    conn.putheader("Content-Length", str(MAX_SETUP_BODY_BYTES + 1))
    conn.endheaders()
    response = conn.getresponse()
    assert response.status == 413
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
    with urlopen(f"http://{parsed.netloc}/dashboard?{parsed.query}", timeout=5):
        pass
    thread.join(timeout=5)
    assert not thread.is_alive()


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
    with urlopen(f"http://{parsed.netloc}/dashboard?{parsed.query}", timeout=5):
        pass
    thread.join(timeout=5)
    assert not thread.is_alive()
