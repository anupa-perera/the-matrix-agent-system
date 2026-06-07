from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from thematrix.prompts import PromptLibrary
from thematrix.prompts.json_tools import extract_json_object
from thematrix.schemas import (
    AgentSpec,
    ModelRequest,
    ModelResponse,
    OracleBrief,
    ProviderConfig,
)
from thematrix.tools import (
    FileDecision,
    FileExecutor,
    FileToolResult,
    ShellCommandResult,
    ShellDecision,
    ShellExecutor,
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
    tool_results: list[ShellCommandResult | FileToolResult] | None = None
    error: str | None = None
    outcome: str = "completed"
    open_questions: list[str] | None = None


class AgentToolRequest(BaseModel):
    kind: Literal["shell", "file_read", "file_write"] = "shell"
    command: str = ""
    path: str = ""
    content: str = ""
    purpose: str = ""


class AgentToolPlan(BaseModel):
    response: str = ""
    tool_requests: list[AgentToolRequest] = Field(default_factory=list)


class AgentFinalAnswer(BaseModel):
    status: Literal["completed", "needs_input", "blocked"] = "completed"
    summary: str = ""
    open_questions: list[str] = Field(default_factory=list)


class AgentRunner:
    """Execute an approved agent spec through the configured model gateway."""

    def __init__(
        self,
        model_gateway: RunnerModelGateway,
        prompt_library: PromptLibrary,
        shell_executor: ShellExecutor | None = None,
        file_executor: FileExecutor | None = None,
    ):
        self.model_gateway = model_gateway
        self.prompt_library = prompt_library
        self.shell_executor = shell_executor
        self.file_executor = file_executor

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
                tool_results=[],
                error=type(exc).__name__,
            )

        plan = self._parse_tool_plan(response.text)
        if not plan.tool_requests:
            final = self._parse_final_answer(plan.response or response.text)
            return AgentExecutionResult(
                executed=True,
                response=final.summary or plan.response or response.text,
                provider_id=response.provider_id,
                model_id=response.model,
                tool_results=[],
                outcome=final.status,
                open_questions=final.open_questions,
            )

        tool_results = self._run_tool_requests(spec, plan.tool_requests)
        final_prompt = self._tool_followup_prompt(blueprint, brief, user_request, plan, tool_results)
        try:
            final_response = self.model_gateway.generate(
                ModelRequest.from_prompt(final_prompt).model_copy(
                    update={
                        "max_tokens": 768,
                        "metadata": {
                            "agent_id": spec.agent_id,
                            "agent_type": spec.agent_type,
                            "tool_results": len(tool_results),
                        },
                    }
                ),
                config=provider_config,
            )
        except Exception as exc:
            return AgentExecutionResult(
                executed=False,
                response=f"Agent execution stopped after tool review: {exc}",
                provider_id=response.provider_id,
                model_id=response.model,
                tool_results=tool_results,
                error=type(exc).__name__,
            )

        final = self._parse_final_answer(final_response.text)
        return AgentExecutionResult(
            executed=True,
            response=final.summary or final_response.text,
            provider_id=final_response.provider_id,
            model_id=final_response.model,
            tool_results=tool_results,
            outcome=final.status,
            open_questions=final.open_questions,
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
            "Respond as the spawned agent. Stay within the blueprint.\n\n"
            "If you need a shell command, return exactly one JSON object in this shape:\n"
            '{"response":"why the command is useful","tool_requests":'
            '[{"kind":"shell","command":"git status -sb","purpose":"Check state"}]}\n\n'
            "For safe file reads use:\n"
            '{"response":"why the file is needed","tool_requests":'
            '[{"kind":"file_read","path":"README.md","purpose":"Read project docs"}]}\n\n'
            "For file writes use `file_write` with `path`, `content`, and `purpose`. "
            "File writes may require user approval.\n\n"
            "Only request commands that fit the blueprint. Do not request commands for secrets.\n\n"
            "When you are giving your final answer (no tool needed), return exactly one JSON "
            "object in this shape:\n"
            '{"status":"completed","summary":"the user-facing answer",'
            '"open_questions":[]}\n'
            "Use `\"needs_input\"` (and list the unresolved decisions in `open_questions`) when "
            "you cannot finish without more information from the user. Use `\"blocked\"` when a "
            "guardrail or missing permission stops you. Only use `\"completed\"` when the task is "
            "actually done."
        )

    def _parse_tool_plan(self, text: str) -> AgentToolPlan:
        try:
            return AgentToolPlan.model_validate(extract_json_object(text))
        except Exception:
            return AgentToolPlan(response=text)

    def _parse_final_answer(self, text: str) -> AgentFinalAnswer:
        try:
            answer = AgentFinalAnswer.model_validate(extract_json_object(text))
        except Exception:
            return AgentFinalAnswer(status="completed", summary=text.strip())
        if not answer.summary.strip():
            answer.summary = text.strip()
        return answer

    def _run_tool_requests(
        self,
        spec: AgentSpec,
        requests: list[AgentToolRequest],
    ) -> list[ShellCommandResult | FileToolResult]:
        results: list[ShellCommandResult | FileToolResult] = []
        for request in requests:
            if request.kind == "shell":
                results.append(self._run_shell_request(spec, request))
            if request.kind == "file_read":
                results.append(self._run_file_read_request(spec, request))
            if request.kind == "file_write":
                results.append(self._run_file_write_request(spec, request))
        return results

    def _run_shell_request(
        self,
        spec: AgentSpec,
        request: AgentToolRequest,
    ) -> ShellCommandResult:
        if "shell_guarded" not in spec.tools_allowed:
            return ShellCommandResult(
                command=request.command,
                purpose=request.purpose,
                decision=ShellDecision.BLOCK,
                reason="This agent spec does not allow shell_guarded.",
            )
        if self.shell_executor is None:
            return ShellCommandResult(
                command=request.command,
                purpose=request.purpose,
                decision=ShellDecision.APPROVAL_REQUIRED,
                reason="Shell execution is not attached in this runtime.",
            )
        return self.shell_executor.run(request.command, purpose=request.purpose)

    def _run_file_read_request(
        self,
        spec: AgentSpec,
        request: AgentToolRequest,
    ) -> FileToolResult:
        if "file_read" not in spec.tools_allowed:
            return FileToolResult(
                operation="read",
                path=request.path,
                purpose=request.purpose,
                decision=FileDecision.BLOCK,
                reason="This agent spec does not allow file_read.",
            )
        if self.file_executor is None:
            return FileToolResult(
                operation="read",
                path=request.path,
                purpose=request.purpose,
                decision=FileDecision.APPROVAL_REQUIRED,
                reason="File execution is not attached in this runtime.",
            )
        return self.file_executor.read(request.path, purpose=request.purpose)

    def _run_file_write_request(
        self,
        spec: AgentSpec,
        request: AgentToolRequest,
    ) -> FileToolResult:
        if "file_write" not in spec.tools_allowed:
            return FileToolResult(
                operation="write",
                path=request.path,
                purpose=request.purpose,
                decision=FileDecision.BLOCK,
                reason="This agent spec does not allow file_write.",
            )
        if self.file_executor is None:
            return FileToolResult(
                operation="write",
                path=request.path,
                purpose=request.purpose,
                decision=FileDecision.APPROVAL_REQUIRED,
                reason="File execution is not attached in this runtime.",
            )
        return self.file_executor.write(
            request.path,
            request.content,
            purpose=request.purpose,
        )

    def _tool_followup_prompt(
        self,
        blueprint: str,
        brief: OracleBrief,
        user_request: str,
        plan: AgentToolPlan,
        tool_results: list[ShellCommandResult | FileToolResult],
    ) -> str:
        result_json = json.dumps(
            [result.model_dump() for result in tool_results],
            indent=2,
        )
        return (
            f"{blueprint}\n\n"
            "# Current Mission\n\n"
            f"User request:\n{user_request.strip()}\n\n"
            "Oracle brief:\n"
            f"{brief.model_dump_json(indent=2)}\n\n"
            "Your tool request summary:\n"
            f"{plan.response}\n\n"
            "Tool results:\n"
            f"{result_json}\n\n"
            "Now give the final answer as exactly one JSON object in this shape:\n"
            '{"status":"completed","summary":"the user-facing answer","open_questions":[]}\n'
            "Use `\"needs_input\"` with `open_questions` when you still need decisions from the "
            "user, or `\"blocked\"` when a guardrail or approval-needed command stopped you. "
            "Mention blocked or approval-needed commands plainly in the summary if they affected "
            "the result."
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
