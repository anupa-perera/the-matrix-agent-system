import json
import re
from threading import Event, Thread
from time import sleep
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from thematrix.config import MatrixPaths
from thematrix.memory import MemoryVault, RuntimeStore
from thematrix.schemas import (
    AgentSpec,
    ClarifyingQuestion,
    EthicalStatus,
    MatrixRunResult,
    MissionTask,
    OperatorGoalKind,
    OperatorGoalStatus,
    OperatorSchedule,
    OracleBrief,
    TaskStatus,
    OperatorGoal,
)
from thematrix.ui.app_server import (
    _agent_page,
    _diagnostics_page,
    _memory_page,
    _message_page,
    _mission_payload,
    _operator_page,
    render_app_page,
    serve_app_ui,
)


def _assert_matrix_background(html: str) -> None:
    assert 'id="matrix-rain"' in html
    assert "getElementById('matrix-rain')" in html or 'getElementById("matrix-rain")' in html


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


class FakeClarifier:
    def __init__(self) -> None:
        self.questions: list[tuple[str, str, str]] = []
        self.intent_checks: list[tuple[str, str, int]] = []
        self.next_questions: list[str] = ["What output should this agent produce?", "READY"]
        self.summaries: list[str] = []

    def answer(self, *, draft, question, target="auto", transcript=None):
        from thematrix.schemas import ClarificationResponse

        self.questions.append((draft, question, target))
        return ClarificationResponse(target=target, answer=f"Clarified: {question}")

    def ask_next(self, *, draft, target="auto", transcript=None):
        from thematrix.schemas import ClarificationResponse

        turns = transcript or []
        self.intent_checks.append((draft, target, len(turns)))
        answer = self.next_questions.pop(0) if self.next_questions else "READY"
        return ClarificationResponse(target=target, answer=answer)

    def summarize(self, *, draft, transcript):
        self.summaries.append(draft)
        return f"SUMMARIZED BRIEF: {draft}"


def test_app_page_renders_request_form(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    store = RuntimeStore(paths.runtime_db)
    store.initialize()

    html = render_app_page(paths, store, "token-123")

    _assert_matrix_background(html)
    assert "/ask?token=token-123" in html
    assert "/dashboard?token=token-123" in html
    assert "Back to Dashboard" in html
    assert "/settings?token=token-123" in html
    assert "Mission Console" in html
    assert "Run Mission" in html
    assert "Intent Check" in html
    assert "Ask Next Question" not in html
    assert "/clarify/intent?token=token-123" not in html
    assert "Intent Transcript" in html
    assert "Clarify first, optional" not in html
    assert "Clarify with" not in html
    assert "Question before running" not in html
    assert "data-mission-form" in html
    assert "Mission accepted. Keep this tab open." in html
    assert "Working. Keep this tab open." in html
    assert "form[method=\"post\"]" in html
    assert html.index("Run Mission") < html.index("Intent Check")
    assert "Help / Commands" in html
    assert "Useful terminal commands" not in html
    assert "the-matrix providers current" not in html
    assert "Recent Missions" in html
    assert "/operator?token=token-123" not in html


def test_oracle_page_answers_general_questions(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    vault = MemoryVault(paths.vault)
    store = RuntimeStore(paths.runtime_db)
    vault.initialize()
    store.initialize()
    captured_url: list[str] = []
    clarifier = FakeClarifier()
    ready = Event()

    def run_server() -> None:
        serve_app_ui(
            paths,
            vault,
            store,
            request_runner=_run_result,
            clarifier=clarifier,
            port=0,
            open_browser=False,
            url_callback=lambda url: (captured_url.append(url), ready.set()),
            timeout_seconds=30,
        )

    thread = Thread(target=run_server, daemon=True)
    thread.start()
    assert ready.wait(timeout=5)
    parsed = urlparse(captured_url[0])

    with urlopen(f"http://{parsed.netloc}/oracle?{parsed.query}", timeout=5) as response:
        oracle_body = response.read().decode("utf-8")

    assert "Ask the Oracle" in oracle_body
    assert "Ask Oracle" in oracle_body

    oracle_request = Request(
        f"http://{parsed.netloc}/oracle/ask?{parsed.query}",
        data=urlencode({"question": "What should I build next?"}).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(oracle_request, timeout=5) as response:
        answer_body = response.read().decode("utf-8")

    assert "Oracle transmission started." in answer_body
    assert "Oracle Signal" in answer_body
    assert "/oracle/status?token=" in answer_body
    match = re.search(r"/oracle/status\?token=[^&\"']+(?:&amp;|&)job_id=([^\"']+)", answer_body)
    assert match is not None
    job_id = match.group(1)

    status = {}
    for _ in range(50):
        with urlopen(
            f"http://{parsed.netloc}/oracle/status?{parsed.query}&job_id={job_id}",
            timeout=5,
        ) as response:
            status = json.loads(response.read().decode("utf-8"))
        if status.get("status") == "completed":
            break
        sleep(0.05)

    assert status["answer"] == "Clarified: What should I build next?"
    assert clarifier.questions == [("", "What should I build next?", "oracle")]

    shutdown_request = Request(
        f"http://{parsed.netloc}/shutdown?{parsed.query}",
        data=b"",
        method="POST",
    )
    with urlopen(shutdown_request, timeout=5):
        pass
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_app_page_recent_mission_links_do_not_use_default_underline(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    store = RuntimeStore(paths.runtime_db)
    store.initialize()
    store.record_run(_run_result("Build a helper").model_copy(update={"run_id": "run-1"}))

    html = render_app_page(paths, store, "token-123")

    assert ".run-link {" in html
    assert ".run-link:hover, .run-link:focus { text-decoration: none; }" in html
    expected_link = (
        '<a class="run-link" href="/mission?token=token-123&run_id=run-1">'
        "<code>run-1</code></a>"
    )
    assert expected_link in html


def test_app_page_renders_operator_goal_controls(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    store = RuntimeStore(paths.runtime_db)
    store.initialize()
    store.upsert_operator_goal(
        OperatorGoal(
            goal_id="goal-1",
            original_request="Send me a notification every 5 minutes",
            title="Drink water",
            kind=OperatorGoalKind.RECURRING_NOTIFICATION,
            status=OperatorGoalStatus.ACTIVE,
            schedule=OperatorSchedule(interval_minutes=5),
            capability="notify_desktop",
            payload={"message": "Drink water"},
        )
    )

    html = _operator_page(store, "token-123")

    _assert_matrix_background(html)
    assert "Drink water" in html
    assert "/operator?token=token-123" in html
    assert "/operator?token=token-123&amp;goal_id=goal-1" in html
    assert "/operator/action?token=token-123" in html
    assert 'value="pause"' in html
    assert 'value="run_now"' in html

    store.upsert_operator_goal(
        OperatorGoal(
            goal_id="goal-2",
            original_request="Send me a notification every 10 minutes",
            title="Pending reminder",
            kind=OperatorGoalKind.RECURRING_NOTIFICATION,
            status=OperatorGoalStatus.PENDING,
            schedule=OperatorSchedule(interval_minutes=10),
            capability="notify_desktop",
            payload={"message": "Pending reminder"},
        )
    )
    html = _operator_page(store, "token-123")
    assert "Pending reminder" in html
    assert 'value="activate"' in html

    store.upsert_operator_goal(
        OperatorGoal(
            goal_id="goal-3",
            original_request="Build a completed helper",
            title="Completed helper",
            kind=OperatorGoalKind.ONE_SHOT,
            status=OperatorGoalStatus.COMPLETED,
            capability="mission_run",
        )
    )
    html = _operator_page(store, "token-123")
    assert "Completed helper" in html

    mission_html = render_app_page(paths, store, "token-123")
    assert "Drink water" not in mission_html
    assert "Pending reminder" not in mission_html


def test_operator_goal_detail_explains_pending_recurring_goal(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    store = RuntimeStore(paths.runtime_db)
    store.initialize()
    store.upsert_operator_goal(
        OperatorGoal(
            goal_id="goal-1",
            original_request="Send me a notification about water every 5 minutes",
            title="Water reminder",
            kind=OperatorGoalKind.RECURRING_NOTIFICATION,
            status=OperatorGoalStatus.PENDING,
            schedule=OperatorSchedule(interval_minutes=5),
            capability="notify_desktop",
            payload={"message": "water"},
        )
    )

    html = _operator_page(store, "token-123", goal_id="goal-1")

    _assert_matrix_background(html)
    assert "Operator Goal" in html
    assert "Nothing recurring will happen until you activate this goal." in html
    assert "After activation" in html
    assert "every 5 minute(s)" in html
    assert "It will not read files, run shell commands, control apps" in html
    assert "Alter Recurring Goal" in html
    assert "/operator/update?token=token-123" in html
    assert 'name="interval_minutes"' in html
    assert 'value="activate"' in html
    assert 'value="cancel"' in html


def test_app_shared_page_shells_include_matrix_background(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    store = RuntimeStore(paths.runtime_db)
    store.initialize()
    store.upsert_agent(AgentSpec(agent_id="test-agent", agent_type="guide", purpose="Test"))

    pages = [
        render_app_page(paths, store, "token-123"),
        _operator_page(store, "token-123"),
        _operator_page(store, "token-123", goal_id="missing-goal"),
        _diagnostics_page(paths, store, "token-123"),
        _memory_page(paths, store, "token-123"),
        _agent_page(paths, store, "token-123", "test-agent"),
        _agent_page(paths, store, "token-123", "missing-agent"),
        _message_page("Forbidden", "Invalid token."),
    ]

    for html in pages:
        _assert_matrix_background(html)


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
    assert "Mission Status" in body
    assert requests == ["Build a tiny helper"]
    assert len(store.list_operator_goals()) == 1

    payload = urlencode({"request": "Send me a notification about stretching every 5 minutes"}).encode(
        "utf-8"
    )
    operator_request = Request(
        f"http://{parsed.netloc}/ask?{parsed.query}",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(operator_request, timeout=5) as response:
        operator_body = response.read().decode("utf-8")

    assert "The Operator drafted a recurring goal" in operator_body
    assert "Open The Operator to review" in operator_body
    assert "The Operator" in operator_body
    assert requests == ["Build a tiny helper"]
    assert len(store.list_operator_goals()) == 2
    pending_goals = [goal for goal in store.list_operator_goals() if goal.title == "stretching"]
    assert pending_goals[0].status == OperatorGoalStatus.PENDING
    assert pending_goals[0].next_run_at is None

    with urlopen(f"http://{parsed.netloc}/operator?{parsed.query}", timeout=5) as response:
        operator_page_body = response.read().decode("utf-8")
    assert "stretching" in operator_page_body

    update_goal_request = Request(
        f"http://{parsed.netloc}/operator/update?{parsed.query}",
        data=urlencode(
            {
                "goal_id": pending_goals[0].goal_id,
                "title": "Stretch reminder",
                "message": "Stretch shoulders",
                "interval_minutes": "15",
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(update_goal_request, timeout=5) as response:
        updated_goal_body = response.read().decode("utf-8")

    assert "Operator goal updated." in updated_goal_body
    updated_pending = store.get_operator_goal(pending_goals[0].goal_id)
    assert updated_pending.title == "Stretch reminder"
    assert updated_pending.payload["message"] == "Stretch shoulders"
    assert updated_pending.schedule.interval_minutes == 15
    assert updated_pending.status == OperatorGoalStatus.PENDING

    activate_request = Request(
        f"http://{parsed.netloc}/operator/action?{parsed.query}",
        data=urlencode(
            {
                "goal_id": pending_goals[0].goal_id,
                "action": "activate",
                "return_to": "dashboard",
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(activate_request, timeout=5) as response:
        activated_body = response.read().decode("utf-8")

    assert "The Matrix Dashboard" in activated_body
    assert "Needs You" in activated_body
    assert store.get_operator_goal(pending_goals[0].goal_id).status == OperatorGoalStatus.ACTIVE

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
    assert "The Operator" in dashboard_body
    assert "/operator?" in dashboard_body
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
    assert "Alter Agent" in agent_body
    assert '.check-row input[type="checkbox"]' in agent_body
    assert "<span>Agent is active</span>" in agent_body
    assert "<span>Reuse this agent for matching future missions</span>" in agent_body

    payload = urlencode(
        {
            "agent_id": "test-agent",
            "purpose": "Guide a normal user through simple tasks.",
            "capabilities": "explain steps\nsummarize results",
            "constraints": "Use plain English",
            "interaction_points": "final_user_handoff",
            "enabled": "on",
            "reusable": "on",
        }
    ).encode("utf-8")
    update_request = Request(
        f"http://{parsed.netloc}/agent/update?{parsed.query}",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(update_request, timeout=5) as response:
        update_body = response.read().decode("utf-8")

    assert "Agent instructions updated." in update_body
    assert store.get_agent("test-agent").purpose == "Guide a normal user through simple tasks."
    assert store.get_agent("test-agent").prompt_block_refs == ["agent-blueprint-test-agent"]
    assert (paths.prompts_dir / "agents" / "test-agent.md").exists()

    payload = urlencode({"agent_id": "test-agent"}).encode("utf-8")
    toggle_request = Request(
        f"http://{parsed.netloc}/agent/toggle?{parsed.query}",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(toggle_request, timeout=5) as response:
        toggle_body = response.read().decode("utf-8")

    assert "Agent paused." in toggle_body
    assert store.get_agent("test-agent").enabled is False

    payload = urlencode({"agent_id": "test-agent", "request": "Use this stored agent"}).encode(
        "utf-8"
    )
    agent_run_request = Request(
        f"http://{parsed.netloc}/agent/run?{parsed.query}",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        urlopen(agent_run_request, timeout=5)
    except HTTPError as exc:
        assert exc.code == 400
        agent_run_body = exc.read().decode("utf-8")
    else:
        raise AssertionError("Paused agent run should return HTTP 400.")

    assert "Resume this agent before running it." in agent_run_body
    assert agent_requests == []

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


def test_app_ui_rejects_oauth_callback_without_valid_state(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    vault = MemoryVault(paths.vault)
    store = RuntimeStore(paths.runtime_db)
    vault.initialize()
    store.initialize()
    captured_url: list[str] = []
    ready = Event()

    def run_server() -> None:
        serve_app_ui(
            paths,
            vault,
            store,
            request_runner=lambda request: _run_result(request),
            port=0,
            open_browser=False,
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
    assert response.getheader("Referrer-Policy") == "no-referrer"
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

    shutdown_request = Request(
        f"http://{parsed.netloc}/shutdown?{parsed.query}",
        data=b"",
        method="POST",
    )
    with urlopen(shutdown_request, timeout=5):
        pass
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_app_ui_clarification_transcript_summarizes_mission_and_agent_runs(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    vault = MemoryVault(paths.vault)
    store = RuntimeStore(paths.runtime_db)
    vault.initialize()
    store.initialize()
    store.upsert_agent(AgentSpec(agent_id="test-agent", agent_type="guide", purpose="Test"))
    captured_url: list[str] = []
    requests: list[str] = []
    agent_requests: list[tuple[str, str]] = []
    clarifier = FakeClarifier()
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
            clarifier=clarifier,
            port=0,
            open_browser=False,
            url_callback=lambda url: (captured_url.append(url), ready.set()),
            timeout_seconds=30,
        )

    thread = Thread(target=run_server, daemon=True)
    thread.start()
    assert ready.wait(timeout=5)
    parsed = urlparse(captured_url[0])

    payload = urlencode({"draft": "Build something", "question": "What is missing?"}).encode(
        "utf-8"
    )
    clarify_without_token = Request(
        f"http://{parsed.netloc}/clarify",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        urlopen(clarify_without_token, timeout=5)
    except HTTPError as exc:
        assert exc.code == 403
    else:
        raise AssertionError("Clarification should require the app token.")

    clarify_request = Request(
        f"http://{parsed.netloc}/clarify?{parsed.query}",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(clarify_request, timeout=5) as response:
        clarify_body = response.read().decode("utf-8")

    assert "Clarification added" in clarify_body
    assert "Clarified: What is missing?" in clarify_body
    assert clarifier.questions == [("Build something", "What is missing?", "auto")]

    mission_request = Request(
        f"http://{parsed.netloc}/ask?{parsed.query}",
        data=urlencode({"request": "Build something"}).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(mission_request, timeout=5) as response:
        response.read()

    for _ in range(50):
        if requests:
            break
        sleep(0.05)
    assert requests == ["SUMMARIZED BRIEF: Build something"]
    for _ in range(50):
        one_shot_goals = [
            goal
            for goal in store.list_operator_goals()
            if goal.kind == OperatorGoalKind.ONE_SHOT
            and goal.status == OperatorGoalStatus.COMPLETED
        ]
        if one_shot_goals:
            break
        sleep(0.05)

    agent_clarify = Request(
        f"http://{parsed.netloc}/clarify?{parsed.query}",
        data=urlencode(
            {
                "context": "agent",
                "agent_id": "test-agent",
                "draft": "Use this stored agent",
                "target": "agent:test-agent",
                "question": "What should this agent confirm?",
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(agent_clarify, timeout=5) as response:
        agent_clarify_body = response.read().decode("utf-8")

    assert "Clarified: What should this agent confirm?" in agent_clarify_body
    assert clarifier.questions[-1] == (
        "Use this stored agent",
        "What should this agent confirm?",
        "agent:test-agent",
    )

    reset_request = Request(
        f"http://{parsed.netloc}/clarify/reset?{parsed.query}",
        data=urlencode({"context": "agent", "agent_id": "test-agent"}).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(reset_request, timeout=5) as response:
        reset_body = response.read().decode("utf-8")

    assert "Clarification transcript cleared." in reset_body
    assert "No intent questions yet" in reset_body

    agent_clarify_again = Request(
        f"http://{parsed.netloc}/clarify?{parsed.query}",
        data=urlencode(
            {
                "context": "agent",
                "agent_id": "test-agent",
                "draft": "Use this stored agent",
                "target": "agent:test-agent",
                "question": "What output is expected?",
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(agent_clarify_again, timeout=5):
        pass

    agent_run = Request(
        f"http://{parsed.netloc}/agent/run?{parsed.query}",
        data=urlencode({"agent_id": "test-agent", "request": "Use this stored agent"}).encode(
            "utf-8"
        ),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(agent_run, timeout=5):
        pass

    for _ in range(50):
        if agent_requests:
            break
        sleep(0.05)
    assert agent_requests == [("test-agent", "SUMMARIZED BRIEF: Use this stored agent")]

    shutdown_request = Request(
        f"http://{parsed.netloc}/shutdown?{parsed.query}",
        data=b"",
        method="POST",
    )
    with urlopen(shutdown_request, timeout=5):
        pass
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_app_ui_intent_check_asks_question_before_mission_runs(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    vault = MemoryVault(paths.vault)
    store = RuntimeStore(paths.runtime_db)
    vault.initialize()
    store.initialize()
    captured_url: list[str] = []
    requests: list[str] = []
    clarifier = FakeClarifier()
    ready = Event()

    def run_server() -> None:
        serve_app_ui(
            paths,
            vault,
            store,
            request_runner=lambda request: requests.append(request) or _run_result(request),
            clarifier=clarifier,
            port=0,
            open_browser=False,
            url_callback=lambda url: (captured_url.append(url), ready.set()),
            timeout_seconds=30,
        )

    thread = Thread(target=run_server, daemon=True)
    thread.start()
    assert ready.wait(timeout=5)
    parsed = urlparse(captured_url[0])

    intent_request = Request(
        f"http://{parsed.netloc}/clarify/intent?{parsed.query}",
        data=urlencode({"draft": "Build something"}).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(intent_request, timeout=5) as response:
        intent_body = response.read().decode("utf-8")

    assert "The Matrix asked the next intent question." in intent_body
    assert "What output should this agent produce?" in intent_body
    assert "Clarification Required" in intent_body
    assert 'role="dialog"' in intent_body
    assert "Your Answer" in intent_body
    assert clarifier.intent_checks == [("Build something", "auto", 0)]

    blocked_run = Request(
        f"http://{parsed.netloc}/ask?{parsed.query}",
        data=urlencode({"request": "Build something"}).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        urlopen(blocked_run, timeout=5)
    except HTTPError as exc:
        blocked_body = exc.read().decode("utf-8")
        assert exc.code == 400
    else:
        raise AssertionError("Mission should not run with an unanswered intent question.")
    assert "Answer the Matrix intent question before running." in blocked_body
    assert "Clarification Required" in blocked_body

    answer_request = Request(
        f"http://{parsed.netloc}/clarify/answer?{parsed.query}",
        data=urlencode({"draft": "Build something", "answer": "A short implementation plan"}).encode(
            "utf-8"
        ),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(answer_request, timeout=5) as response:
        answer_body = response.read().decode("utf-8")

    assert "The Matrix has enough context. You can run this mission now." in answer_body
    assert "A short implementation plan" in answer_body
    assert clarifier.intent_checks[-1] == ("Build something", "auto", 2)

    run_request = Request(
        f"http://{parsed.netloc}/ask?{parsed.query}",
        data=urlencode({"request": "Build something"}).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(run_request, timeout=5):
        pass

    for _ in range(50):
        if requests:
            break
        sleep(0.05)
    assert requests == ["SUMMARIZED BRIEF: Build something"]

    shutdown_request = Request(
        f"http://{parsed.netloc}/shutdown?{parsed.query}",
        data=b"",
        method="POST",
    )
    with urlopen(shutdown_request, timeout=5):
        pass
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_app_ui_intake_gate_collects_answers_before_launch(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    vault = MemoryVault(paths.vault)
    store = RuntimeStore(paths.runtime_db)
    vault.initialize()
    store.initialize()
    captured_url: list[str] = []
    requests: list[str] = []
    clarifier = FakeClarifier()
    questions = [
        ClarifyingQuestion(
            id="coverage",
            question="All listed stocks or a watchlist?",
            why="Sets the agent's coverage.",
            options=["All", "Watchlist"],
            recommended="Watchlist",
        )
    ]
    ready = Event()

    def run_server() -> None:
        serve_app_ui(
            paths,
            vault,
            store,
            request_runner=lambda request: requests.append(request) or _run_result(request),
            intake_runner=lambda draft: questions,
            clarifier=clarifier,
            port=0,
            open_browser=False,
            url_callback=lambda url: (captured_url.append(url), ready.set()),
            timeout_seconds=30,
        )

    thread = Thread(target=run_server, daemon=True)
    thread.start()
    assert ready.wait(timeout=5)
    parsed = urlparse(captured_url[0])

    ask_request = Request(
        f"http://{parsed.netloc}/ask?{parsed.query}",
        data=urlencode({"request": "Research CSE stocks"}).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(ask_request, timeout=5) as response:
        ask_body = response.read().decode("utf-8")

    assert "Mission Intake" in ask_body
    assert "All listed stocks or a watchlist?" in ask_body
    assert "Start Mission" in ask_body
    assert "Accept Recommended Defaults" in ask_body
    assert "/intake/submit?token=" in ask_body
    assert requests == []  # Nothing spawned until the user answers.

    submit_request = Request(
        f"http://{parsed.netloc}/intake/submit?{parsed.query}",
        data=urlencode(
            {
                "request": "Research CSE stocks",
                "question_keys": "coverage",
                "qtext__coverage": "All listed stocks or a watchlist?",
                "ans__coverage": "Watchlist",
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(submit_request, timeout=5) as response:
        submit_body = response.read().decode("utf-8")

    assert "Mission Status" in submit_body
    for _ in range(50):
        if requests:
            break
        sleep(0.05)
    assert requests == ["SUMMARIZED BRIEF: Research CSE stocks"]
    assert clarifier.summaries == ["Research CSE stocks"]

    shutdown_request = Request(
        f"http://{parsed.netloc}/shutdown?{parsed.query}",
        data=b"",
        method="POST",
    )
    with urlopen(shutdown_request, timeout=5):
        pass
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_app_ui_surfaces_runtime_approval_and_resumes_after_response(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    vault = MemoryVault(paths.vault)
    store = RuntimeStore(paths.runtime_db)
    vault.initialize()
    store.initialize()
    captured_url: list[str] = []
    approvals: list[bool] = []
    ready = Event()

    def approval_runner(request: str, approval_callback=None) -> MatrixRunResult:
        assert approval_callback is not None
        approvals.append(
            approval_callback(
                "pip install example-package",
                "Command needs explicit user approval.",
                "Install dependency.",
            )
        )
        return _run_result(request)

    def run_server() -> None:
        serve_app_ui(
            paths,
            vault,
            store,
            request_runner=approval_runner,
            port=0,
            open_browser=False,
            url_callback=lambda url: (captured_url.append(url), ready.set()),
            timeout_seconds=30,
        )

    thread = Thread(target=run_server, daemon=True)
    thread.start()
    assert ready.wait(timeout=5)
    parsed = urlparse(captured_url[0])

    mission_request = Request(
        f"http://{parsed.netloc}/ask?{parsed.query}",
        data=urlencode({"request": "Needs approval"}).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(mission_request, timeout=5) as response:
        body = response.read().decode("utf-8")

    match = re.search(r"job_id=([^\"&]+)", body)
    assert match is not None
    job_id = match.group(1)

    status = {}
    for _ in range(50):
        with urlopen(
            f"http://{parsed.netloc}/mission/status?{parsed.query}&job_id={job_id}",
            timeout=5,
        ) as response:
            status = json.loads(response.read().decode("utf-8"))
        if status.get("approvals"):
            break
        sleep(0.05)

    assert status["status"] == "running"
    assert status["approvals"][0]["target"] == "pip install example-package"
    assert status["approvals"][0]["purpose"] == "Install dependency."
    approval_id = status["approvals"][0]["approval_id"]

    with urlopen(f"http://{parsed.netloc}/dashboard?{parsed.query}", timeout=5) as response:
        dashboard_body = response.read().decode("utf-8")

    assert "Approval needed" in dashboard_body
    assert "pip install example-package" in dashboard_body
    assert "/approval/respond" in dashboard_body
    assert 'value="approve"' in dashboard_body
    assert 'value="deny"' in dashboard_body

    approve_request = Request(
        f"http://{parsed.netloc}/approval/respond?{parsed.query}",
        data=urlencode(
            {
                "approval_id": approval_id,
                "decision": "approve",
                "return_to": "dashboard",
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(approve_request, timeout=5) as response:
        approval_body = response.read().decode("utf-8")

    assert "Recent Missions" in approval_body
    assert 'value="approve"' not in approval_body

    for _ in range(50):
        with urlopen(
            f"http://{parsed.netloc}/mission/status?{parsed.query}&job_id={job_id}",
            timeout=5,
        ) as response:
            status = json.loads(response.read().decode("utf-8"))
        if status["status"] == "completed":
            break
        sleep(0.05)

    assert approvals == [True]
    assert status["status"] == "completed"
    assert any(event["stage"] == "approval_required" for event in status["events"])
    assert any(event["stage"] == "approval_granted" for event in status["events"])

    shutdown_request = Request(
        f"http://{parsed.netloc}/shutdown?{parsed.query}",
        data=b"",
        method="POST",
    )
    with urlopen(shutdown_request, timeout=5):
        pass
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_app_ui_exposes_pollable_mission_status(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    vault = MemoryVault(paths.vault)
    store = RuntimeStore(paths.runtime_db)
    vault.initialize()
    store.initialize()
    store.upsert_agent(AgentSpec(agent_id="test-agent", agent_type="guide", purpose="Test"))
    captured_url: list[str] = []
    ready = Event()
    running = Event()
    release = Event()

    def slow_result(request: str, progress_callback=None) -> MatrixRunResult:
        if progress_callback is not None:
            progress_callback("oracle", "Oracle is reading the request.", {})
        running.set()
        assert release.wait(timeout=5)
        result = _run_result(request)
        store.record_run(result)
        return result

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
    request = Request(
        f"http://{parsed.netloc}/ask?{parsed.query}",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        body = response.read().decode("utf-8")

    assert "Mission Status" in body
    match = re.search(r"job_id=([^\"&]+)", body)
    assert match is not None
    job_id = match.group(1)
    assert running.wait(timeout=5)

    with urlopen(
        f"http://{parsed.netloc}/mission/status?{parsed.query}&job_id={job_id}",
        timeout=5,
    ) as response:
        status = json.loads(response.read().decode("utf-8"))

    assert status["status"] == "running"
    assert any(event["stage"] == "oracle" for event in status["events"])

    release.set()
    for _ in range(100):
        with urlopen(
            f"http://{parsed.netloc}/mission/status?{parsed.query}&job_id={job_id}",
            timeout=5,
        ) as response:
            status = json.loads(response.read().decode("utf-8"))
        if status["status"] == "completed":
            break
        sleep(0.05)
    assert status["status"] == "completed"
    assert status["result"]["response"] == "Mission complete."

    with urlopen(
        f"http://{parsed.netloc}/mission?{parsed.query}&run_id={status['result']['run_id']}",
        timeout=5,
    ) as response:
        detail_body = response.read().decode("utf-8")

    assert "Mission Status" in detail_body
    assert "Mission complete." in detail_body
    assert "Ask This Agent" in detail_body
    assert "/agent?" in detail_body
    assert "agent_id=test-agent" in detail_body

    shutdown_request = Request(
        f"http://{parsed.netloc}/shutdown?{parsed.query}",
        data=b"",
        method="POST",
    )
    with urlopen(shutdown_request, timeout=5):
        pass
    server_thread.join(timeout=5)
    assert not server_thread.is_alive()


def test_mission_status_payload_explains_execution_path(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    store = RuntimeStore(paths.runtime_db)
    store.initialize()
    spec = AgentSpec(
        agent_id="news-agent",
        agent_type="operator",
        purpose="Send configurable local news updates.",
        capabilities=["confirm_preferences", "summarize_news"],
        tools_allowed=["memory_read"],
        constraints=["Ask before background scheduling."],
        interaction_points=["before_sensitive_actions", "final_user_handoff"],
    )
    result = MatrixRunResult(
        request="create a news update agent",
        oracle_brief=OracleBrief(
            intent="create a news update agent",
            ethical_status=EthicalStatus.SAFE,
            user_interaction_required=True,
            human_need="Make the operating path clear before setup.",
            constraints=["Respect the user's privacy mode."],
            success_criteria=[
                "The user can see what each agent step is for.",
                "The agent stays inside its approved scope.",
            ],
        ),
        agent_spec=spec,
        response="Mission complete.",
        metadata={
            "mission_strategy": "sequential",
            "mission_task_count": 1,
            "mission_completed_count": 1,
            "agent_execution_status": "executed",
        },
    )
    store.record_run(result)
    store.record_mission_task(
        result.run_id,
        MissionTask(
            sequence=1,
            title="Confirm news preferences",
            description="Ask the user which topics, sources, and delivery mode they want.",
            agent_spec=spec,
            architect_decision="Spawned a focused operator for the preference step.",
            required_capabilities=["confirm_preferences"],
            expected_outputs=["User-approved news topics, sources, and delivery mode."],
            completion_checks=["The user knows what must happen before scheduling starts."],
            user_actions=["Approve background scheduling before it is enabled."],
            status=TaskStatus.COMPLETED,
            result_summary="Collected user-approved topics and delivery mode.",
        ),
    )

    payload = _mission_payload(store, None, run_id=result.run_id)

    assert payload["mission_context"]["need"] == "Make the operating path clear before setup."
    assert payload["mission_context"]["success_criteria"][0] == (
        "The user can see what each agent step is for."
    )
    assert payload["tasks"][0]["description"] == (
        "Ask the user which topics, sources, and delivery mode they want."
    )
    assert payload["tasks"][0]["agent_purpose"] == "Send configurable local news updates."
    assert payload["tasks"][0]["capabilities"] == ["confirm_preferences", "summarize_news"]
    assert payload["tasks"][0]["required_capabilities"] == ["confirm_preferences"]
    assert payload["tasks"][0]["expected_outputs"] == [
        "User-approved news topics, sources, and delivery mode."
    ]
    assert payload["tasks"][0]["completion_checks"] == [
        "The user knows what must happen before scheduling starts."
    ]
    assert payload["tasks"][0]["user_actions"] == [
        "Approve background scheduling before it is enabled."
    ]
    assert payload["tasks"][0]["interaction_points"] == [
        "before_sensitive_actions",
        "final_user_handoff",
    ]
    assert payload["tasks"][0]["result_summary"] == (
        "Collected user-approved topics and delivery mode."
    )


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
