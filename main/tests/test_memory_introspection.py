from __future__ import annotations

from thematrix.memory import RuntimeStore
from thematrix.schemas import AgentSpec, MatrixRunResult, OracleBrief, RiskLevel, SecurityReport


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
    store.record_prompt_block("agent-blueprint-builder-test-agent", "agent_blueprint", "prompt")
    store.record_model_call(
        provider_id="openrouter",
        model="openai/gpt-5-mini",
        ok=True,
        request_chars=4,
        response_chars=5,
        latency_ms=12,
    )
    store.record_run(
        MatrixRunResult(
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
    )

    agents = store.list_agent_records()
    prompt_blocks = store.list_prompt_blocks()
    model_calls = store.list_model_calls()
    security_events = store.list_security_events()

    assert agents[0]["agent_id"] == "builder-test-agent"
    assert agents[0]["success_count"] == 1
    assert agents[0]["failure_count"] == 0
    assert store.get_agent("builder-test-agent") == spec
    assert prompt_blocks[0]["block_ref"] == "agent-blueprint-builder-test-agent"
    assert len(prompt_blocks[0]["content_hash"]) == 64
    assert model_calls[0]["provider_id"] == "openrouter"
    assert model_calls[0]["request_chars"] == 4
    assert security_events[0]["approved"] == 1
    assert security_events[0]["issues"] == []
