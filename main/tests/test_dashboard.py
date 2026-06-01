from thematrix.memory import RuntimeStore
from thematrix.schemas import (
    AgentSpec,
    AuthMode,
    MatrixRunResult,
    OperatorGoal,
    OperatorGoalKind,
    OperatorGoalStatus,
    OperatorSchedule,
    OracleBrief,
    ProviderConfig,
)
from thematrix.ui.dashboard import render_dashboard_html
from thematrix.config import MatrixPaths


def test_dashboard_renders_local_memory_summary(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    store = RuntimeStore(paths.runtime_db)
    store.initialize()
    store.configure_provider(
        ProviderConfig(
            provider_id="ollama",
            selected_model="llama3.2",
            auth_mode=AuthMode.NONE,
        )
    )
    store.record_provider_verification("ollama", ok=True, message="ready", model="llama3.2")
    store.record_run(
        MatrixRunResult(
            run_id="run-1",
            request="Build a helper",
            response="done",
            oracle_brief=OracleBrief(
                intent="Build a helper",
                ethical_status="safe",
                user_interaction_required=True,
                human_need="Be clear.",
            ),
            metadata={
                "runtime": "nebuchadnezzar",
                "mission_task_count": 1,
                "mission_completed_count": 0,
            },
        )
    )

    html = render_dashboard_html(paths, store)

    assert 'id="matrix-rain"' in html
    assert "getElementById('matrix-rain')" in html
    assert "The Matrix Dashboard" in html
    assert '<link rel="icon" type="image/svg+xml"' in html
    assert "data:image/svg+xml" in html
    assert "Control Center" in html
    assert "the-matrix start" in html
    assert "Build a helper" in html
    assert "Verification: ok" in html
    assert str(paths.vault) in html


def test_dashboard_renders_live_app_actions(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    store = RuntimeStore(paths.runtime_db)
    store.initialize()
    store.upsert_agent(AgentSpec(agent_id="test-agent", agent_type="guide", purpose="Test"))

    html = render_dashboard_html(paths, store, app_token="token-123")

    assert "Mission Console" in html
    assert "The Operator" in html
    assert "/?token=token-123" in html
    assert "/operator?token=token-123" in html
    assert "/settings?token=token-123" in html
    assert "/diagnostics?token=token-123" in html
    assert "/memory?token=token-123" in html
    assert "/shutdown?token=token-123" in html
    assert "/agent?token=token-123&amp;agent_id=test-agent" in html
    assert "/mission?token=token-123&amp;run_id=" not in html
    assert "run this agent" in html


def test_dashboard_renders_operator_goal_panel(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    store = RuntimeStore(paths.runtime_db)
    store.initialize()
    store.upsert_operator_goal(
        OperatorGoal(
            goal_id="goal-1",
            original_request="Send me a notification about water every 5 minutes",
            title="Water reminder",
            kind=OperatorGoalKind.RECURRING_NOTIFICATION,
            status=OperatorGoalStatus.ACTIVE,
            schedule=OperatorSchedule(interval_minutes=5),
            payload={"message": "water"},
        )
    )
    store.upsert_operator_goal(
        OperatorGoal(
            goal_id="goal-2",
            original_request="Build helper",
            title="Completed helper",
            kind=OperatorGoalKind.ONE_SHOT,
            status=OperatorGoalStatus.COMPLETED,
            capability="mission_run",
        )
    )

    html = render_dashboard_html(paths, store, app_token="token-123")

    assert "Water reminder" in html
    assert "active" in html
    assert "inspect goal" in html
    assert "/operator?token=token-123&amp;goal_id=goal-1" in html
    assert "Completed helper" not in html


def test_dashboard_links_live_mission_details(tmp_path) -> None:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    store = RuntimeStore(paths.runtime_db)
    store.initialize()
    store.record_run(
        MatrixRunResult(
            run_id="run-1",
            request="Build a helper",
            response="done",
            oracle_brief=OracleBrief(
                intent="Build a helper",
                ethical_status="safe",
                user_interaction_required=True,
                human_need="Be clear.",
            ),
            metadata={
                "runtime": "nebuchadnezzar",
                "mission_task_count": 1,
                "mission_completed_count": 1,
            },
        )
    )

    html = render_dashboard_html(paths, store, app_token="token-123")

    assert "/mission?token=token-123&amp;run_id=run-1" in html
    assert ".mission-card {" in html
    assert ".mission-card:hover code" in html
    expected_link = (
        '<a class="mission-card" href="/mission?token=token-123&amp;run_id=run-1">'
        "<code>run-1</code></a>"
    )
    assert expected_link in html
