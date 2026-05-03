from __future__ import annotations

from pathlib import Path

from thematrix.oracle import Oracle
from thematrix.prompts import PromptLibrary
from thematrix.schemas import ModelRequest, ModelResponse


class FakeGateway:
    def __init__(self, text: str):
        self.text = text
        self.requests: list[ModelRequest] = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(provider_id="fake", model="fake-model", text=self.text)


def test_oracle_uses_model_when_json_is_valid() -> None:
    gateway = FakeGateway(
        """
        {
          "intent": "Build a planning agent",
          "ethical_status": "safe",
          "user_interaction_required": true,
          "human_need": "Explain decisions clearly.",
          "constraints": ["Keep scope clear."],
          "success_criteria": ["The user understands the plan."]
        }
        """
    )

    brief = Oracle(model_gateway=gateway).assess("Build a planning agent")

    assert brief.intent == "Build a planning agent"
    assert brief.ethical_status == "safe"
    assert gateway.requests


def test_oracle_falls_back_when_model_json_is_invalid() -> None:
    oracle = Oracle(model_gateway=FakeGateway("not json"))

    brief = oracle.assess("Delete a system secret")

    assert brief.ethical_status == "sensitive"
    assert oracle.last_assessment_source == "heuristic_fallback"


def test_prompt_library_installs_markdown_defaults(tmp_path: Path) -> None:
    prompt_dir = tmp_path / "prompts"
    library = PromptLibrary(prompt_dir)

    library.install_defaults()

    prompt_path = prompt_dir / "oracle_assess.md"
    assert prompt_path.exists()
    assert "Oracle Intent And Ethics Pass" in prompt_path.read_text(encoding="utf-8")

