from threading import Event, Thread
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from thematrix.config import MatrixPaths
from thematrix.memory import MemoryVault, RuntimeStore
from thematrix.schemas import (
    AgentSpec,
    EthicalStatus,
    MatrixRunResult,
    OracleBrief,
)
from thematrix.ui.app_server import render_app_page, serve_app_ui


def _run_result(request: str) -> MatrixRunResult:
    return MatrixRunResult(
        request=request,
        oracle_brief=OracleBrief(
            intent="test",
            ethical_status=EthicalStatus.SAFE,
            user_interaction_required=False,
            human_need="clear answer",
        ),
        agent_spec=AgentSpec(agent_id="test-agent", agent_type="guide", purpose="Test"),
        response="Mission complete.",
    )


def test_app_page_renders_request_form(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    store = RuntimeStore(paths.runtime_db)
    store.initialize()

    html = render_app_page(paths, store, "token-123")

    assert "/ask?token=token-123" in html
    assert "/dashboard?token=token-123" in html
    assert "Back to Dashboard" in html
    assert "/settings?token=token-123" in html
    assert "Transmit Request" in html
    assert "data-mission-form" in html
    assert "Mission accepted. Keep this tab open." in html
    assert "Help / Commands" in html
    assert "the-matrix providers current" in html
    assert "Recent Missions" in html


def test_app_ui_server_runs_browser_request(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    vault = MemoryVault(paths.vault)
    store = RuntimeStore(paths.runtime_db)
    vault.initialize()
    store.initialize()
    store.upsert_agent(AgentSpec(agent_id="test-agent", agent_type="guide", purpose="Test"))
    captured_url: list[str] = []
    requests: list[str] = []
    agent_requests: list[tuple[str, str]] = []
    ready = Event()

    def run_server() -> None:
        serve_app_ui(
            paths,
            vault,
            store,
            request_runner=lambda request: requests.append(request) or _run_result(request),
            agent_request_runner=lambda agent_id, request: (
                agent_requests.append((agent_id, request)) or _run_result(request)
            ),
            port=0,
            open_browser=False,
            url_callback=lambda url: (captured_url.append(url), ready.set()),
            timeout_seconds=30,
        )

    thread = Thread(target=run_server, daemon=True)
    thread.start()
    assert ready.wait(timeout=5)
    parsed = urlparse(captured_url[0])
    assert parsed.path == "/dashboard"

    try:
        urlopen(f"http://{parsed.netloc}/", timeout=5)
    except HTTPError as exc:
        assert exc.code == 403
    else:
        raise AssertionError("App UI allowed a request without the session token.")

    payload = urlencode({"request": "Build a tiny helper"}).encode("utf-8")
    request = Request(
        f"http://{parsed.netloc}/ask?{parsed.query}",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        body = response.read().decode("utf-8")

    assert "Mission complete." in body
    assert requests == ["Build a tiny helper"]

    with urlopen(f"http://{parsed.netloc}/settings?{parsed.query}", timeout=5) as response:
        settings_body = response.read().decode("utf-8")
    assert "Connect a model" in settings_body
    assert "Start here" in settings_body
    assert "Back to Dashboard" in settings_body
    assert "Sign in with OpenRouter" in settings_body
    assert f"/dashboard?{parsed.query}" in settings_body
    assert "/save?" in settings_body

    payload = urlencode(
        {
            "provider_id": "ollama",
            "model": "llama3.2",
            "auth_mode": "none",
            "privacy_mode": "local_only",
            "file_change_consent": "ask_each_time",
        }
    ).encode("utf-8")
    save_request = Request(
        f"http://{parsed.netloc}/save?{parsed.query}",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(save_request, timeout=5) as response:
        save_body = response.read().decode("utf-8")

    assert "Saved setup" in save_body
    assert store.get_default_provider_config() is not None

    with urlopen(f"http://{parsed.netloc}/dashboard?{parsed.query}", timeout=5) as response:
        dashboard_body = response.read().decode("utf-8")
    assert "Control Center" in dashboard_body
    assert "/diagnostics?" in dashboard_body
    assert "/memory?" in dashboard_body
    assert "/agent?" in dashboard_body

    with urlopen(
        f"http://{parsed.netloc}/agent?{parsed.query}&agent_id=test-agent",
        timeout=5,
    ) as response:
        agent_body = response.read().decode("utf-8")
    assert "Run Agent" in agent_body
    assert "test-agent" in agent_body
    assert "data-mission-form" in agent_body

    payload = urlencode({"agent_id": "test-agent", "request": "Use this stored agent"}).encode(
        "utf-8"
    )
    agent_run_request = Request(
        f"http://{parsed.netloc}/agent/run?{parsed.query}",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(agent_run_request, timeout=5) as response:
        agent_run_body = response.read().decode("utf-8")

    assert "Mission complete." in agent_run_body
    assert agent_requests == [("test-agent", "Use this stored agent")]

    with urlopen(f"http://{parsed.netloc}/diagnostics?{parsed.query}", timeout=5) as response:
        diagnostics_body = response.read().decode("utf-8")
    assert "System Check" in diagnostics_body
    assert "Back to Dashboard" in diagnostics_body
    assert "Secrets backend" in diagnostics_body

    with urlopen(f"http://{parsed.netloc}/memory?{parsed.query}", timeout=5) as response:
        memory_body = response.read().decode("utf-8")
    assert "Back to Dashboard" in memory_body
    assert "The Matrix stores readable memory" in memory_body
    assert str(paths.vault) in memory_body

    shutdown_request = Request(
        f"http://{parsed.netloc}/shutdown?{parsed.query}",
        data=b"",
        method="POST",
    )
    with urlopen(shutdown_request, timeout=5) as response:
        shutdown_body = response.read().decode("utf-8")

    thread.join(timeout=5)
    assert "App stopped" in shutdown_body
    assert not thread.is_alive()


def test_app_ui_duplicate_submit_reports_running_state(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    vault = MemoryVault(paths.vault)
    store = RuntimeStore(paths.runtime_db)
    vault.initialize()
    store.initialize()
    captured_url: list[str] = []
    ready = Event()
    running = Event()
    release = Event()

    def slow_result(request: str) -> MatrixRunResult:
        running.set()
        assert release.wait(timeout=5)
        return _run_result(request)

    def run_server() -> None:
        serve_app_ui(
            paths,
            vault,
            store,
            request_runner=slow_result,
            port=0,
            open_browser=False,
            url_callback=lambda url: (captured_url.append(url), ready.set()),
            timeout_seconds=30,
        )

    server_thread = Thread(target=run_server, daemon=True)
    server_thread.start()
    assert ready.wait(timeout=5)
    parsed = urlparse(captured_url[0])

    payload = urlencode({"request": "Long mission"}).encode("utf-8")
    first_request = Request(
        f"http://{parsed.netloc}/ask?{parsed.query}",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    first_done = Event()

    def send_first_request() -> None:
        with urlopen(first_request, timeout=10) as response:
            response.read()
        first_done.set()

    first_thread = Thread(target=send_first_request, daemon=True)
    first_thread.start()
    assert running.wait(timeout=5)

    second_request = Request(
        f"http://{parsed.netloc}/ask?{parsed.query}",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        urlopen(second_request, timeout=5)
    except HTTPError as exc:
        body = exc.read().decode("utf-8")
        assert exc.code == 409
    else:
        raise AssertionError("Duplicate mission submit should return HTTP 409.")

    assert "Mission Running" in body
    assert "did not start a duplicate mission" in body

    release.set()
    assert first_done.wait(timeout=5)

    shutdown_request = Request(
        f"http://{parsed.netloc}/shutdown?{parsed.query}",
        data=b"",
        method="POST",
    )
    with urlopen(shutdown_request, timeout=5):
        pass
    server_thread.join(timeout=5)
    assert not server_thread.is_alive()
