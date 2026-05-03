from __future__ import annotations

from pathlib import Path

from thematrix.architect import Architect
from thematrix.memory import RuntimeStore
from thematrix.prompts import PromptLibrary
from thematrix.schemas import ModelRequest, ModelResponse, OracleBrief, PrivacyMode


class FakeGateway:
    def __init__(self, text: str | list[str]):
        self.responses = text if isinstance(text, list) else [text]
        self.requests: list[ModelRequest] = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        return ModelResponse(provider_id="fake", model="fake-model", text=self.responses[index])


def test_architect_uses_model_draft_but_sanitizes_runtime_power(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    prompt_library = PromptLibrary(tmp_path / "prompts")
    prompt_library.install_defaults()
    gateway = FakeGateway(
        """
        {
          "agent_type": "builder",
          "purpose": "Implement scoped local software changes safely.",
          "capabilities": ["inspect_project", "edit_files"],
          "tools_allowed": ["file_read", "file_write", "shell_guarded", "secrets_read"],
          "memory_scope": ["wiki/agents/", "raw/secrets/"],
          "constraints": ["Keep changes small."],
          "interaction_points": ["before_file_writes"],
          "risk_level": "high",
          "reusable": true
        }
        """
    )
    architect = Architect(store, model_gateway=gateway, prompt_library=prompt_library)
    brief = OracleBrief(
        intent="Build a code editing agent",
        ethical_status="safe",
        user_interaction_required=True,
        human_need="Explain changes simply.",
    )

    spec = architect.design_agent(brief, privacy_mode=PrivacyMode.ASK_EACH_TIME)

    assert architect.last_design_source == "model"
    assert spec.agent_type == "builder"
    assert spec.purpose == "Implement scoped local software changes safely."
    assert "secrets_read" not in spec.tools_allowed
    assert "shell_guarded" not in spec.tools_allowed
    assert spec.memory_scope == ["wiki/agents/"]
    assert "final_user_handoff" in spec.interaction_points
    assert (tmp_path / "prompts" / "agents" / f"{spec.agent_id}.md").exists()
    with store.connect() as conn:
        row = conn.execute(
            "SELECT block_type FROM prompt_blocks WHERE block_ref = ?",
            (spec.prompt_block_refs[0],),
        ).fetchone()
    assert row["block_type"] == "agent_blueprint"


def test_architect_falls_back_when_model_draft_is_invalid(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    architect = Architect(store, model_gateway=FakeGateway("not json"))
    brief = OracleBrief(
        intent="Research local model options",
        ethical_status="safe",
        user_interaction_required=False,
        human_need="Keep it concise.",
    )

    spec = architect.design_agent(brief)

    assert architect.last_design_source == "heuristic_fallback"
    assert spec.agent_type == "researcher"


def test_architect_reuses_existing_agent_for_same_purpose(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    architect = Architect(store)
    brief = OracleBrief(
        intent="Build a coding helper agent",
        ethical_status="safe",
        user_interaction_required=True,
        human_need="Guide the user clearly.",
    )

    first = architect.design_agent(brief)
    second = architect.design_agent(brief)

    assert first.agent_id == second.agent_id
    assert second.reuse_candidate_id == first.agent_id


def test_architect_reuses_similar_agent_without_rewriting_blueprint(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    prompt_library = PromptLibrary(tmp_path / "prompts")
    prompt_library.install_defaults()
    first_architect = Architect(store, prompt_library=prompt_library)
    first_brief = OracleBrief(
        intent="Build a coding helper agent",
        ethical_status="safe",
        user_interaction_required=True,
        human_need="Guide the user clearly.",
    )
    first = first_architect.design_agent(first_brief)
    first_blueprint = prompt_library.read_agent_blueprint(first.agent_id)
    gateway = FakeGateway(
        """
        {
          "agent_type": "builder",
          "purpose": "Implement requested local software changes.",
          "capabilities": ["inspect_project", "edit_files", "run_checks"],
          "tools_allowed": ["file_read", "file_write", "shell_guarded"],
          "memory_scope": ["wiki/agents/", "wiki/workflows/"],
          "constraints": ["Keep changes scoped."],
          "interaction_points": ["before_file_writes"],
          "risk_level": "medium",
          "reusable": true
        }
        """
    )
    second_brief = OracleBrief(
        intent="Implement a small repo change",
        ethical_status="safe",
        user_interaction_required=True,
        human_need="Keep it clear.",
    )

    second = Architect(
        store,
        model_gateway=gateway,
        prompt_library=prompt_library,
    ).design_agent(second_brief)

    assert second.agent_id == first.agent_id
    assert second.reuse_candidate_id == first.agent_id
    assert second.purpose == first.purpose
    assert second.tools_allowed == first.tools_allowed
    assert prompt_library.read_agent_blueprint(first.agent_id) == first_blueprint


def test_architect_does_not_reuse_when_candidate_has_more_tools(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    prompt_library = PromptLibrary(tmp_path / "prompts")
    prompt_library.install_defaults()
    first = Architect(store, prompt_library=prompt_library).design_agent(
        OracleBrief(
            intent="Build a coding helper agent",
            ethical_status="safe",
            user_interaction_required=True,
            human_need="Guide the user clearly.",
        )
    )
    gateway = FakeGateway(
        """
        {
          "agent_type": "builder",
          "purpose": "Plan and implement scoped local software changes.",
          "capabilities": ["inspect_project"],
          "tools_allowed": ["file_read"],
          "memory_scope": ["wiki/agents/"],
          "constraints": [],
          "interaction_points": ["before_file_writes"],
          "risk_level": "medium",
          "reusable": true
        }
        """
    )

    limited = Architect(
        store,
        model_gateway=gateway,
        prompt_library=prompt_library,
    ).design_agent(
        OracleBrief(
            intent="Inspect code without changing files",
            ethical_status="safe",
            user_interaction_required=False,
            human_need="Be concise.",
        )
    )

    assert limited.agent_id != first.agent_id
    assert limited.reuse_candidate_id is None
    assert limited.tools_allowed == ["file_read"]


def test_architect_plans_build_then_sentinel_review(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    brief = OracleBrief(
        intent="Build a coding helper agent",
        ethical_status="safe",
        user_interaction_required=True,
        human_need="Guide the user clearly.",
    )

    plan = Architect(store).plan_mission(brief)

    assert plan.strategy == "sequential"
    assert [task.agent_spec.agent_type for task in plan.tasks] == ["builder", "sentinel"]
    assert plan.tasks[0].sequence == 1
    assert plan.tasks[1].sequence == 2
    assert "Spawned" in plan.tasks[0].architect_decision
    assert plan.tasks[0].agent_spec.agent_id in plan.tasks[0].architect_decision


def test_architect_records_reuse_decision_on_mission_task(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    architect = Architect(store)
    brief = OracleBrief(
        intent="Build a coding helper agent",
        ethical_status="safe",
        user_interaction_required=True,
        human_need="Guide the user clearly.",
    )
    first = architect.plan_mission(brief)

    second = architect.plan_mission(brief)

    assert second.tasks[0].agent_spec.agent_id == first.tasks[0].agent_spec.agent_id
    assert "Reused" in second.tasks[0].architect_decision
    assert "risk/tool bounds" in second.tasks[0].architect_decision


def test_architect_uses_model_for_sequential_plan_then_sanitizes_specs(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    prompt_library = PromptLibrary(tmp_path / "prompts")
    prompt_library.install_defaults()
    gateway = FakeGateway(
        [
            """
            {
              "tasks": [
                "Research the repository structure.",
                "Implement the requested change.",
                "Review the change for safety risks."
              ]
            }
            """,
            """
            {
              "agent_type": "researcher",
              "purpose": "Research repository structure.",
              "capabilities": ["inspect_project"],
              "tools_allowed": ["memory_read", "provider_call"],
              "memory_scope": ["wiki/agents/"],
              "constraints": [],
              "interaction_points": ["final_user_handoff"],
              "risk_level": "low",
              "reusable": true
            }
            """,
            """
            {
              "agent_type": "builder",
              "purpose": "Implement requested local changes.",
              "capabilities": ["edit_files"],
              "tools_allowed": ["file_read", "file_write", "shell_guarded"],
              "memory_scope": ["wiki/agents/"],
              "constraints": [],
              "interaction_points": ["before_file_writes"],
              "risk_level": "medium",
              "reusable": true
            }
            """,
            """
            {
              "agent_type": "sentinel",
              "purpose": "Review changes for safety risks.",
              "capabilities": ["audit_outputs"],
              "tools_allowed": ["memory_read", "security_policy_read"],
              "memory_scope": ["wiki/risks/"],
              "constraints": [],
              "interaction_points": ["final_user_handoff"],
              "risk_level": "medium",
              "reusable": true
            }
            """,
        ]
    )
    brief = OracleBrief(
        intent="Research and implement a repo change",
        ethical_status="safe",
        user_interaction_required=True,
        human_need="Keep it clear.",
    )

    plan = Architect(store, model_gateway=gateway, prompt_library=prompt_library).plan_mission(
        brief
    )

    assert [task.agent_spec.agent_type for task in plan.tasks] == [
        "researcher",
        "builder",
        "sentinel",
    ]
    assert len(gateway.requests) == 4
