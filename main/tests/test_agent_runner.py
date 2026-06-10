from __future__ import annotations

from thematrix.architect import Architect
from thematrix.memory import MemoryVault, RuntimeStore
from thematrix.neo import Neo
from thematrix.operator import TheOperator
from thematrix.oracle import Oracle
from thematrix.prompts import PromptLibrary
from thematrix.runtime import AgentRunner, Nebuchadnezzar
from thematrix.schemas import (
    AgentSpec,
    AuthMode,
    ModelRequest,
    ModelResponse,
    OperatorGoalKind,
    OperatorGoalStatus,
    PrivacyMode,
    ProviderConfig,
    ToolCall,
)
from thematrix.tools import FileExecutor, NotificationResult, ShellExecutor


class FakeGateway:
    def __init__(self, text: str | list[str]):
        self.responses = text if isinstance(text, list) else [text]
        self.requests: list[ModelRequest] = []
        self.configs: list[ProviderConfig | None] = []

    def generate(
        self,
        request: ModelRequest,
        config: ProviderConfig | None = None,
    ) -> ModelResponse:
        self.requests.append(request)
        self.configs.append(config)
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        return ModelResponse(provider_id="ollama", model="local-test", text=self.responses[index])


def test_runtime_executes_spawned_agent_when_provider_is_configured(tmp_path) -> None:
    vault = MemoryVault(tmp_path / "vault")
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    prompt_library = PromptLibrary(tmp_path / "prompts")
    vault.initialize()
    store.initialize()
    prompt_library.install_defaults()
    gateway = FakeGateway("Here is the spawned agent answer.")
    runtime = Nebuchadnezzar(
        oracle=Oracle(),
        architect=Architect(store, prompt_library=prompt_library),
        neo=Neo(),
        vault=vault,
        store=store,
        agent_runner=AgentRunner(gateway, prompt_library),
    )

    result = runtime.run(
        "Build a planning agent",
        privacy_mode=PrivacyMode.ASK_EACH_TIME,
        provider_config=ProviderConfig(
            provider_id="ollama",
            selected_model="local-test",
            auth_mode=AuthMode.NONE,
        ),
    )

    assert result.metadata["agent_execution_status"] == "executed"
    assert result.metadata["agent_execution_error"] is None
    assert result.metadata["mission_task_count"] == 2
    assert result.metadata["mission_completed_count"] == 2
    assert "Final sequential result" in result.response
    assert "Here is the spawned agent answer." in result.response
    assert gateway.requests
    assert gateway.configs[0] is not None
    assert gateway.configs[0].provider_id == "ollama"
    assert "# Agent Blueprint" in gateway.requests[0].messages[0].content
    records = store.list_agent_records()
    assert sum(record["success_count"] for record in records) == 2
    assert sum(record["failure_count"] for record in records) == 0
    tasks = store.list_mission_tasks(result.run_id)
    assert len(tasks) == 2
    assert [task.status.value for task in tasks] == ["completed", "completed"]
    assert [task.agent_spec.agent_type for task in tasks] == ["builder", "sentinel"]


def test_runtime_continues_skipped_mission_with_current_provider(tmp_path) -> None:
    vault = MemoryVault(tmp_path / "vault")
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    prompt_library = PromptLibrary(tmp_path / "prompts")
    vault.initialize()
    store.initialize()
    prompt_library.install_defaults()
    first_runtime = Nebuchadnezzar(
        oracle=Oracle(),
        architect=Architect(store, prompt_library=prompt_library),
        neo=Neo(),
        vault=vault,
        store=store,
    )
    initial = first_runtime.run(
        "Build a planning agent",
        privacy_mode=PrivacyMode.ASK_EACH_TIME,
    )
    assert [task.status.value for task in store.list_mission_tasks(initial.run_id)] == [
        "skipped",
        "skipped",
    ]
    gateway = FakeGateway("continued task completed")
    continued_runtime = Nebuchadnezzar(
        oracle=Oracle(),
        architect=Architect(store, prompt_library=prompt_library),
        neo=Neo(),
        vault=vault,
        store=store,
        agent_runner=AgentRunner(gateway, prompt_library),
    )

    continued = continued_runtime.continue_mission(
        initial.run_id,
        privacy_mode=PrivacyMode.ASK_EACH_TIME,
        provider_config=ProviderConfig(
            provider_id="ollama",
            selected_model="local-test",
            auth_mode=AuthMode.NONE,
        ),
    )

    assert continued.run_id == initial.run_id
    assert continued.metadata["resumed"] is True
    assert continued.metadata["mission_completed_count"] == 2
    tasks = store.list_mission_tasks(initial.run_id)
    assert [task.status.value for task in tasks] == ["completed", "completed"]
    assert all(task.agent_spec.provider_id == "ollama" for task in tasks)
    assert len(gateway.requests) == 2


def test_runtime_runs_selected_reusable_agent_directly(tmp_path) -> None:
    vault = MemoryVault(tmp_path / "vault")
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    prompt_library = PromptLibrary(tmp_path / "prompts")
    vault.initialize()
    store.initialize()
    prompt_library.install_defaults()
    spec = Architect(store, prompt_library=prompt_library).design_agent(
        Oracle().assess("Build a planning agent"),
        provider_config=ProviderConfig(
            provider_id="ollama",
            selected_model="local-test",
            auth_mode=AuthMode.NONE,
        ),
    )
    gateway = FakeGateway("manual agent completed")
    runtime = Nebuchadnezzar(
        oracle=Oracle(),
        architect=Architect(store, prompt_library=prompt_library),
        neo=Neo(),
        vault=vault,
        store=store,
        agent_runner=AgentRunner(gateway, prompt_library),
    )

    result = runtime.run_agent(
        spec.agent_id,
        "Use this exact agent",
        privacy_mode=PrivacyMode.ASK_EACH_TIME,
        provider_config=ProviderConfig(
            provider_id="ollama",
            selected_model="local-test",
            auth_mode=AuthMode.NONE,
        ),
    )

    assert result.metadata["manual_agent_run"] is True
    assert result.metadata["selected_agent_id"] == spec.agent_id
    assert result.metadata["mission_strategy"] == "manual_agent"
    assert result.metadata["mission_task_count"] == 1
    assert result.metadata["mission_completed_count"] == 1
    assert "Manual agent run" in result.response
    assert "manual agent completed" in result.response
    tasks = store.list_mission_tasks(result.run_id)
    assert len(tasks) == 1
    assert tasks[0].agent_spec.agent_id == spec.agent_id
    assert len(gateway.requests) == 1


def test_runtime_refuses_paused_manual_agent(tmp_path) -> None:
    vault = MemoryVault(tmp_path / "vault")
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    prompt_library = PromptLibrary(tmp_path / "prompts")
    vault.initialize()
    store.initialize()
    prompt_library.install_defaults()
    spec = Architect(store, prompt_library=prompt_library).design_agent(
        Oracle().assess("Build a planning agent")
    )
    store.upsert_agent(spec.model_copy(update={"enabled": False}))
    gateway = FakeGateway("should not run")
    runtime = Nebuchadnezzar(
        oracle=Oracle(),
        architect=Architect(store, prompt_library=prompt_library),
        neo=Neo(),
        vault=vault,
        store=store,
        agent_runner=AgentRunner(gateway, prompt_library),
    )

    try:
        runtime.run_agent(
            spec.agent_id,
            "Use this stored agent",
            privacy_mode=PrivacyMode.ASK_EACH_TIME,
        )
    except ValueError as exc:
        assert "paused" in str(exc)
    else:
        raise AssertionError("Paused agents should not run.")
    assert gateway.requests == []


def test_agent_runner_executes_allowed_shell_tool_and_returns_final_answer(tmp_path) -> None:
    prompt_library = PromptLibrary(tmp_path / "prompts")
    prompt_library.install_defaults()
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    spec = Architect(store, prompt_library=prompt_library).design_agent(
        Oracle().assess("Build a planning agent"),
        provider_config=ProviderConfig(
            provider_id="ollama",
            selected_model="local-test",
            auth_mode=AuthMode.NONE,
        ),
    )
    gateway = FakeGateway(
        [
            """
            {
              "response": "I need to check Python availability.",
              "tool_requests": [
                {
                  "kind": "shell",
                  "command": "python --version",
                  "purpose": "Check Python availability."
                }
              ]
            }
            """,
            "Python is available, so the agent can continue.",
        ]
    )

    execution = AgentRunner(
        gateway,
        prompt_library,
        shell_executor=ShellExecutor(cwd=tmp_path),
    ).run(
        spec,
        Oracle().assess("Build a planning agent"),
        "Build a planning agent",
    )

    assert execution.executed
    assert execution.response == "Python is available, so the agent can continue."
    assert execution.tool_results is not None
    assert execution.tool_results[0].executed
    assert execution.tool_results[0].command == "python --version"
    assert len(gateway.requests) == 2
    assert "Tool results" in gateway.requests[1].messages[0].content


def test_agent_runner_executes_allowed_file_read_tool(tmp_path) -> None:
    prompt_library = PromptLibrary(tmp_path / "prompts")
    prompt_library.install_defaults()
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    (tmp_path / "notes.md").write_text("local note", encoding="utf-8")
    spec = Architect(store, prompt_library=prompt_library).design_agent(
        Oracle().assess("Build a planning agent"),
        provider_config=ProviderConfig(
            provider_id="ollama",
            selected_model="local-test",
            auth_mode=AuthMode.NONE,
        ),
    )
    gateway = FakeGateway(
        [
            """
            {
              "response": "I need to read the local note.",
              "tool_requests": [
                {
                  "kind": "file_read",
                  "path": "notes.md",
                  "purpose": "Read the local note."
                }
              ]
            }
            """,
            "I read the local note.",
        ]
    )

    execution = AgentRunner(
        gateway,
        prompt_library,
        file_executor=FileExecutor(tmp_path),
    ).run(
        spec,
        Oracle().assess("Build a planning agent"),
        "Build a planning agent",
    )

    assert execution.executed
    assert execution.tool_results is not None
    assert execution.tool_results[0].executed
    assert "local note" in execution.tool_results[0].model_dump_json()
    assert "Tool results" in gateway.requests[1].messages[0].content


def test_agent_runner_reports_needs_input_outcome(tmp_path) -> None:
    prompt_library = PromptLibrary(tmp_path / "prompts")
    prompt_library.install_defaults()
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    spec = Architect(store, prompt_library=prompt_library).design_agent(
        Oracle().assess("Build a planning agent"),
        provider_config=ProviderConfig(
            provider_id="ollama",
            selected_model="local-test",
            auth_mode=AuthMode.NONE,
        ),
    )
    gateway = FakeGateway(
        '{"status":"needs_input","summary":"I need the coverage scope.",'
        '"open_questions":["All stocks or a watchlist?"]}'
    )

    execution = AgentRunner(gateway, prompt_library).run(
        spec,
        Oracle().assess("Build a planning agent"),
        "Build a planning agent",
    )

    assert execution.executed
    assert execution.outcome == "needs_input"
    assert execution.open_questions == ["All stocks or a watchlist?"]
    assert execution.response == "I need the coverage scope."


def test_runtime_blocks_when_agent_needs_user_input(tmp_path) -> None:
    vault = MemoryVault(tmp_path / "vault")
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    prompt_library = PromptLibrary(tmp_path / "prompts")
    vault.initialize()
    store.initialize()
    prompt_library.install_defaults()
    gateway = FakeGateway(
        '{"status":"needs_input","summary":"I need the coverage scope.",'
        '"open_questions":["All stocks or a watchlist?"]}'
    )
    runtime = Nebuchadnezzar(
        oracle=Oracle(),
        architect=Architect(store, prompt_library=prompt_library),
        neo=Neo(),
        vault=vault,
        store=store,
        agent_runner=AgentRunner(gateway, prompt_library),
    )

    result = runtime.run(
        "Build a planning agent",
        privacy_mode=PrivacyMode.ASK_EACH_TIME,
        provider_config=ProviderConfig(
            provider_id="ollama",
            selected_model="local-test",
            auth_mode=AuthMode.NONE,
        ),
    )

    tasks = store.list_mission_tasks(result.run_id)
    assert tasks[0].status.value == "blocked"
    assert "All stocks or a watchlist?" in (tasks[0].error or "")
    assert result.metadata["mission_completed_count"] == 0
    assert "needs your input" in result.response
    # The mission stops after the blocked task instead of cascading.
    assert tasks[1].status.value == "pending"
    assert len(gateway.requests) == 1


class NativeToolFakeGateway:
    """Returns scripted ModelResponse objects, mimicking native tool calling."""

    def __init__(self, responses: list[ModelResponse]):
        self.responses = responses
        self.requests: list[ModelRequest] = []

    def generate(
        self,
        request: ModelRequest,
        config: ProviderConfig | None = None,
    ) -> ModelResponse:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        return self.responses[index]


def test_agent_runner_prefers_native_tool_calls(tmp_path) -> None:
    prompt_library = PromptLibrary(tmp_path / "prompts")
    prompt_library.install_defaults()
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    spec = Architect(store, prompt_library=prompt_library).design_agent(
        Oracle().assess("Build a planning agent"),
        provider_config=ProviderConfig(
            provider_id="ollama",
            selected_model="local-test",
            auth_mode=AuthMode.NONE,
        ),
    )
    gateway = NativeToolFakeGateway(
        [
            ModelResponse(
                provider_id="ollama",
                model="local-test",
                text="",
                tool_calls=[
                    ToolCall(
                        name="shell",
                        arguments={"command": "python --version", "purpose": "Check Python"},
                    )
                ],
            ),
            ModelResponse(
                provider_id="ollama",
                model="local-test",
                text='{"status":"completed","summary":"Python is ready.","open_questions":[]}',
            ),
        ]
    )

    execution = AgentRunner(
        gateway,
        prompt_library,
        shell_executor=ShellExecutor(cwd=tmp_path),
    ).run(
        spec,
        Oracle().assess("Build a planning agent"),
        "Build a planning agent",
    )

    assert execution.executed
    assert execution.response == "Python is ready."
    assert execution.tool_results is not None
    assert execution.tool_results[0].command == "python --version"
    # The runner advertised native tool schemas for the allowed tools.
    sent_tools = {tool.name for tool in gateway.requests[0].tools}
    assert "shell" in sent_tools
    assert "file_read" in sent_tools
    assert "schedule" not in sent_tools


def test_agent_runner_reports_unknown_native_tool_back_to_model(tmp_path) -> None:
    prompt_library = PromptLibrary(tmp_path / "prompts")
    prompt_library.install_defaults()
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    spec = Architect(store, prompt_library=prompt_library).design_agent(
        Oracle().assess("Build a planning agent"),
        provider_config=ProviderConfig(
            provider_id="ollama",
            selected_model="local-test",
            auth_mode=AuthMode.NONE,
        ),
    )
    gateway = NativeToolFakeGateway(
        [
            ModelResponse(
                provider_id="ollama",
                model="local-test",
                text="",
                tool_calls=[ToolCall(name="launch_rocket", arguments={})],
            ),
            ModelResponse(
                provider_id="ollama",
                model="local-test",
                text='{"status":"blocked","summary":"That tool is unavailable.",'
                '"open_questions":[]}',
            ),
        ]
    )

    execution = AgentRunner(gateway, prompt_library).run(
        spec,
        Oracle().assess("Build a planning agent"),
        "Build a planning agent",
    )

    assert execution.executed
    assert execution.tool_results is not None
    assert not execution.tool_results[0].executed
    assert "launch_rocket" in execution.tool_results[0].reason


def test_agent_runner_emits_live_progress_events(tmp_path) -> None:
    prompt_library = PromptLibrary(tmp_path / "prompts")
    prompt_library.install_defaults()
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    spec = Architect(store, prompt_library=prompt_library).design_agent(
        Oracle().assess("Build a planning agent"),
        provider_config=ProviderConfig(
            provider_id="ollama",
            selected_model="local-test",
            auth_mode=AuthMode.NONE,
        ),
    )
    gateway = FakeGateway(
        [
            '{"response":"Check Python.","tool_requests":'
            '[{"kind":"shell","command":"python --version","purpose":"Check Python"}]}',
            '{"status":"completed","summary":"Done.","open_questions":[]}',
        ]
    )
    events: list[tuple[str, str]] = []

    AgentRunner(
        gateway,
        prompt_library,
        shell_executor=ShellExecutor(cwd=tmp_path),
    ).run(
        spec,
        Oracle().assess("Build a planning agent"),
        "Build a planning agent",
        progress_callback=lambda stage, message, details: events.append((stage, message)),
    )

    stages = [stage for stage, _ in events]
    assert "agent_thinking" in stages
    assert "agent_tools" in stages
    assert "agent_tool_result" in stages
    tool_result_messages = [message for stage, message in events if stage == "agent_tool_result"]
    assert any("python --version" in message for message in tool_result_messages)


def test_mission_timeline_receives_agent_tool_events(tmp_path) -> None:
    vault = MemoryVault(tmp_path / "vault")
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    prompt_library = PromptLibrary(tmp_path / "prompts")
    vault.initialize()
    store.initialize()
    prompt_library.install_defaults()
    gateway = FakeGateway(
        [
            '{"response":"Check Python.","tool_requests":'
            '[{"kind":"shell","command":"python --version","purpose":"Check Python"}]}',
            '{"status":"completed","summary":"Done.","open_questions":[]}',
        ]
    )
    progress: list[tuple[str, str, dict]] = []
    runtime = Nebuchadnezzar(
        oracle=Oracle(),
        architect=Architect(store, prompt_library=prompt_library),
        neo=Neo(),
        vault=vault,
        store=store,
        agent_runner=AgentRunner(
            gateway,
            prompt_library,
            shell_executor=ShellExecutor(cwd=tmp_path),
        ),
        progress_callback=lambda stage, message, details: progress.append(
            (stage, message, details)
        ),
    )

    runtime.run(
        "Build a planning agent",
        privacy_mode=PrivacyMode.ASK_EACH_TIME,
        provider_config=ProviderConfig(
            provider_id="ollama",
            selected_model="local-test",
            auth_mode=AuthMode.NONE,
        ),
    )

    stages = [stage for stage, _, _ in progress]
    assert "agent_tools" in stages
    assert "agent_tool_result" in stages
    tool_events = [details for stage, _, details in progress if stage == "agent_tool_result"]
    assert all("task_id" in details and "agent_id" in details for details in tool_events)


def test_agent_runner_loops_through_multiple_tool_rounds(tmp_path) -> None:
    prompt_library = PromptLibrary(tmp_path / "prompts")
    prompt_library.install_defaults()
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    (tmp_path / "notes.md").write_text("local note", encoding="utf-8")
    spec = Architect(store, prompt_library=prompt_library).design_agent(
        Oracle().assess("Build a planning agent"),
        provider_config=ProviderConfig(
            provider_id="ollama",
            selected_model="local-test",
            auth_mode=AuthMode.NONE,
        ),
    )
    gateway = FakeGateway(
        [
            '{"response":"Check Python first.","tool_requests":'
            '[{"kind":"shell","command":"python --version","purpose":"Check Python"}]}',
            '{"response":"Now read the note.","tool_requests":'
            '[{"kind":"file_read","path":"notes.md","purpose":"Read the note"}]}',
            '{"status":"completed","summary":"Checked Python and read the note.",'
            '"open_questions":[]}',
        ]
    )

    execution = AgentRunner(
        gateway,
        prompt_library,
        shell_executor=ShellExecutor(cwd=tmp_path),
        file_executor=FileExecutor(tmp_path),
    ).run(
        spec,
        Oracle().assess("Build a planning agent"),
        "Build a planning agent",
    )

    assert execution.executed
    assert execution.response == "Checked Python and read the note."
    assert execution.tool_results is not None
    assert len(execution.tool_results) == 2
    assert len(gateway.requests) == 3
    assert "Tool round 1" in gateway.requests[1].messages[0].content
    assert "Tool round 2" in gateway.requests[2].messages[0].content


def test_agent_runner_stops_when_tool_batch_repeats(tmp_path) -> None:
    prompt_library = PromptLibrary(tmp_path / "prompts")
    prompt_library.install_defaults()
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    spec = Architect(store, prompt_library=prompt_library).design_agent(
        Oracle().assess("Build a planning agent"),
        provider_config=ProviderConfig(
            provider_id="ollama",
            selected_model="local-test",
            auth_mode=AuthMode.NONE,
        ),
    )
    # The gateway keeps returning the same tool request forever.
    gateway = FakeGateway(
        '{"response":"Check Python.","tool_requests":'
        '[{"kind":"shell","command":"python --version","purpose":"Check Python"}]}'
    )

    execution = AgentRunner(
        gateway,
        prompt_library,
        shell_executor=ShellExecutor(cwd=tmp_path),
    ).run(
        spec,
        Oracle().assess("Build a planning agent"),
        "Build a planning agent",
    )

    assert execution.executed
    assert execution.tool_results is not None
    assert len(execution.tool_results) == 1
    # One planning round, one repeated round, one forced final answer.
    assert len(gateway.requests) == 3


def test_agent_runner_schedules_recurring_goal_through_operator(tmp_path) -> None:
    prompt_library = PromptLibrary(tmp_path / "prompts")
    prompt_library.install_defaults()
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    spec = AgentSpec(
        agent_id="operator-scheduler-test",
        agent_type="operator",
        purpose="Coordinate recurring follow-up work.",
        tools_allowed=["memory_read", "notify_desktop", "operator_schedule"],
        prompt_block_refs=["agent-blueprint-operator-scheduler-test"],
    )
    gateway = FakeGateway(
        [
            '{"response":"The user wants an hourly check.","tool_requests":'
            '[{"kind":"schedule","mission":"Check disk space and tidy temp files",'
            '"interval_minutes":60,"purpose":"User asked for hourly cleanup"}]}',
            '{"status":"completed","summary":"Scheduled the hourly disk check.",'
            '"open_questions":[]}',
        ]
    )

    class FakeNotifier:
        def send(self, title: str, message: str) -> NotificationResult:
            return NotificationResult(ok=True, message="sent")

    execution = AgentRunner(
        gateway,
        prompt_library,
        notifier=FakeNotifier(),
        scheduler=TheOperator(store, notifier=FakeNotifier()),
    ).run(
        spec,
        Oracle().assess("Check disk space every hour"),
        "Check disk space every hour",
    )

    assert execution.executed
    assert execution.response == "Scheduled the hourly disk check."
    assert execution.tool_results is not None
    assert execution.tool_results[0].executed
    assert execution.tool_results[0].operation == "schedule"
    goals = store.list_operator_goals()
    assert len(goals) == 1
    assert goals[0].kind == OperatorGoalKind.RECURRING_MISSION
    assert goals[0].status == OperatorGoalStatus.ACTIVE
    assert goals[0].schedule is not None
    assert goals[0].schedule.interval_minutes == 60
    assert goals[0].payload["mission_request"] == "Check disk space and tidy temp files"


def test_agent_runner_blocks_schedule_tool_without_permission(tmp_path) -> None:
    prompt_library = PromptLibrary(tmp_path / "prompts")
    prompt_library.install_defaults()
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    spec = AgentSpec(
        agent_id="operator-no-schedule-test",
        agent_type="operator",
        purpose="Coordinate a simple request.",
        tools_allowed=["memory_read"],
        prompt_block_refs=["agent-blueprint-operator-no-schedule-test"],
    )
    gateway = FakeGateway(
        [
            '{"response":"Trying to schedule.","tool_requests":'
            '[{"kind":"schedule","mission":"Check disk","interval_minutes":60,'
            '"purpose":"test"}]}',
            '{"status":"blocked","summary":"Scheduling is not allowed for this agent.",'
            '"open_questions":[]}',
        ]
    )

    execution = AgentRunner(
        gateway,
        prompt_library,
        scheduler=TheOperator(store),
    ).run(
        spec,
        Oracle().assess("Check disk space every hour"),
        "Check disk space every hour",
    )

    assert execution.executed
    assert execution.tool_results is not None
    assert not execution.tool_results[0].executed
    assert "does not allow operator_schedule" in execution.tool_results[0].reason
    assert store.list_operator_goals() == []


def test_agent_runner_returns_error_result_when_gateway_fails(tmp_path) -> None:
    prompt_library = PromptLibrary(tmp_path / "prompts")
    prompt_library.install_defaults()
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    spec = Architect(store, prompt_library=prompt_library).design_agent(
        Oracle().assess("Build a planning agent"),
        provider_config=ProviderConfig(
            provider_id="ollama",
            selected_model="local-test",
            auth_mode=AuthMode.NONE,
        ),
    )

    class FailingGateway:
        def generate(
            self,
            request: ModelRequest,
            config: ProviderConfig | None = None,
        ) -> ModelResponse:
            raise RuntimeError("offline")

    execution = AgentRunner(FailingGateway(), prompt_library).run(
        spec,
        Oracle().assess("Build a planning agent"),
        "Build a planning agent",
    )

    assert not execution.executed
    assert execution.error == "RuntimeError"
    assert "offline" in execution.response
