from __future__ import annotations

from pathlib import Path

from thematrix.architect import Architect
from thematrix.memory import RuntimeStore
from thematrix.prompts import PromptLibrary
from thematrix.schemas import ModelRequest, ModelResponse, OracleBrief, PrivacyMode


class FakeGateway:
    def __init__(self, text: str):
        self.text = text
        self.requests: list[ModelRequest] = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(provider_id="fake", model="fake-model", text=self.text)


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
