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
    assert "/settings?token=token-123" in html
    assert "Transmit Request" in html
    assert "Help / Commands" in html
    assert "the-matrix providers current" in html
    assert "Recent Missions" in html


def test_app_ui_server_runs_browser_request(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    vault = MemoryVault(paths.vault)
    store = RuntimeStore(paths.runtime_db)
    vault.initialize()
    store.initialize()
    captured_url: list[str] = []
    requests: list[str] = []
    ready = Event()

    def run_server() -> None:
        serve_app_ui(
            paths,
            vault,
            store,
            request_runner=lambda request: requests.append(request) or _run_result(request),
            port=0,
            open_browser=False,
            url_callback=lambda url: (captured_url.append(url), ready.set()),
            timeout_seconds=30,
        )

    thread = Thread(target=run_server, daemon=True)
    thread.start()
    assert ready.wait(timeout=5)
    parsed = urlparse(captured_url[0])

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
    assert "Model Interface" in settings_body
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

    # Let the timeout close the server without making this test wait for it.
    # The thread is daemonized because the app UI is intentionally long-lived.
