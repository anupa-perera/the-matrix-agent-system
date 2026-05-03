from __future__ import annotations

from thematrix.memory import RuntimeStore
from thematrix.schemas import (
    AgentSpec,
    MatrixRunResult,
    MissionTask,
    OracleBrief,
    RiskLevel,
    SecurityReport,
    TaskStatus,
)


def test_runtime_store_lists_agents_prompt_blocks_model_calls_and_security_events(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    spec = AgentSpec(
        agent_id="builder-test-agent",
        agent_type="builder",
        purpose="Plan and implement scoped local software changes.",
        risk_level=RiskLevel.MEDIUM,
        tools_allowed=["file_read"],
        memory_scope=["wiki/agents/"],
        prompt_block_refs=["agent-blueprint-builder-test-agent"],
    )

    store.upsert_agent(spec)
    store.record_agent_outcome("builder-test-agent", success=True)
    task = MissionTask(
        sequence=1,
        title="Build helper",
        description="Build a helper",
        agent_spec=spec,
        status=TaskStatus.COMPLETED,
        result_summary="done",
    )
    store.record_mission_task("run-1", task)
    store.record_prompt_block("agent-blueprint-builder-test-agent", "agent_blueprint", "prompt")
    store.record_model_call(
        provider_id="openrouter",
        model="openai/gpt-5-mini",
        ok=True,
        request_chars=4,
        response_chars=5,
        latency_ms=12,
    )
    run = MatrixRunResult(
        run_id="run-1",
        request="Build a helper",
        response="done",
        oracle_brief=OracleBrief(
            intent="Build a helper",
            ethical_status="safe",
            user_interaction_required=True,
            human_need="Be clear.",
        ),
        preflight_report=SecurityReport(
            approved=True,
            risk_level=RiskLevel.MEDIUM,
        ),
    )
    store.record_run(run)
    store.record_run(
        run.model_copy(
            update={
                "response": "updated",
            }
        )
    )

    agents = store.list_agent_records()
    reusable_agents = store.list_reusable_agents("builder")
    store.touch_agent("builder-test-agent")
    runs = store.list_run_records()
    stored_run = store.get_run("run-1")
    prompt_blocks = store.list_prompt_blocks()
    model_calls = store.list_model_calls()
    security_events = store.list_security_events()
    mission_tasks = store.list_mission_tasks("run-1")

    assert agents[0]["agent_id"] == "builder-test-agent"
    assert agents[0]["success_count"] == 1
    assert agents[0]["failure_count"] == 0
    assert store.get_agent("builder-test-agent") == spec
    assert reusable_agents == [spec]
    assert runs[0]["run_id"] == "run-1"
    assert stored_run is not None
    assert stored_run.response == "updated"
    assert prompt_blocks[0]["block_ref"] == "agent-blueprint-builder-test-agent"
    assert len(prompt_blocks[0]["content_hash"]) == 64
    assert model_calls[0]["provider_id"] == "openrouter"
    assert model_calls[0]["request_chars"] == 4
    assert security_events[0]["approved"] == 1
    assert security_events[0]["issues"] == []
    assert mission_tasks[0].task_id == task.task_id
    assert mission_tasks[0].status == TaskStatus.COMPLETED
