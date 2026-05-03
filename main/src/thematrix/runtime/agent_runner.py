from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from thematrix.prompts import PromptLibrary
from thematrix.schemas import (
    AgentSpec,
    ModelRequest,
    ModelResponse,
    OracleBrief,
    ProviderConfig,
)


class RunnerModelGateway(Protocol):
    def generate(
        self,
        request: ModelRequest,
        config: ProviderConfig | None = None,
    ) -> ModelResponse: ...


@dataclass
class AgentExecutionResult:
    executed: bool
    response: str
    provider_id: str
    model_id: str
    error: str | None = None


class AgentRunner:
    """Execute an approved agent spec through the configured model gateway."""

    def __init__(
        self,
        model_gateway: RunnerModelGateway,
        prompt_library: PromptLibrary,
    ):
        self.model_gateway = model_gateway
        self.prompt_library = prompt_library

    def run(
        self,
        spec: AgentSpec,
        brief: OracleBrief,
        user_request: str,
        provider_config: ProviderConfig | None = None,
    ) -> AgentExecutionResult:
        try:
            blueprint = self.prompt_library.read_agent_blueprint(spec.agent_id)
        except FileNotFoundError:
            blueprint = self._fallback_blueprint(spec)

        prompt = self._execution_prompt(blueprint, brief, user_request)
        try:
            response = self.model_gateway.generate(
                ModelRequest.from_prompt(prompt).model_copy(
                    update={
                        "max_tokens": 768,
                        "metadata": {"agent_id": spec.agent_id, "agent_type": spec.agent_type},
                    }
                ),
                config=provider_config,
            )
        except Exception as exc:
            return AgentExecutionResult(
                executed=False,
                response=f"Agent execution could not start: {exc}",
                provider_id=spec.provider_id,
                model_id=spec.model_id,
                error=type(exc).__name__,
            )

        return AgentExecutionResult(
            executed=True,
            response=response.text,
            provider_id=response.provider_id,
            model_id=response.model,
        )

    def _execution_prompt(
        self,
        blueprint: str,
        brief: OracleBrief,
        user_request: str,
    ) -> str:
        return (
            f"{blueprint}\n\n"
            "# Current Mission\n\n"
            f"User request:\n{user_request.strip()}\n\n"
            "Oracle brief:\n"
            f"{brief.model_dump_json(indent=2)}\n\n"
            "Respond as the spawned agent. Stay within the blueprint. "
            "If the request requires tools that are not available yet, explain the next safe step."
        )

    def _fallback_blueprint(self, spec: AgentSpec) -> str:
        return (
            f"# Agent Blueprint: {spec.agent_id}\n\n"
            f"Type: {spec.agent_type}\n\n"
            f"Purpose: {spec.purpose}\n\n"
            "Use only these tools:\n"
            f"{self._markdown_list(spec.tools_allowed)}\n\n"
            "Memory scope:\n"
            f"{self._markdown_list(spec.memory_scope)}\n"
        )

    def _markdown_list(self, values: list[str]) -> str:
        if not values:
            return "- None"
        return "\n".join(f"- {value}" for value in values)
