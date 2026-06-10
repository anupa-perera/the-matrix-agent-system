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
    NotificationResult,
    OperatorToolResult,
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


class AgentNotifier(Protocol):
    def send(self, title: str, message: str) -> NotificationResult: ...


class GoalScheduler(Protocol):
    def create_recurring_notification_goal(
        self,
        original_request: str,
        message: str,
        interval_minutes: int,
        activate: bool = True,
    ) -> object: ...

    def create_recurring_mission_goal(
        self,
        original_request: str,
        mission_request: str,
        interval_minutes: int,
        activate: bool = True,
    ) -> object: ...


AgentToolResult = ShellCommandResult | FileToolResult | OperatorToolResult


@dataclass
class AgentExecutionResult:
    executed: bool
    response: str
    provider_id: str
    model_id: str
    tool_results: list[AgentToolResult] | None = None
    error: str | None = None
    outcome: str = "completed"
    open_questions: list[str] | None = None


class AgentToolRequest(BaseModel):
    kind: Literal["shell", "file_read", "file_write", "notify", "schedule"] = "shell"
    command: str = ""
    path: str = ""
    content: str = ""
    message: str = ""
    mission: str = ""
    interval_minutes: int = 0
    purpose: str = ""


class AgentToolPlan(BaseModel):
    response: str = ""
    tool_requests: list[AgentToolRequest] = Field(default_factory=list)


class AgentFinalAnswer(BaseModel):
    status: Literal["completed", "needs_input", "blocked"] = "completed"
    summary: str = ""
    open_questions: list[str] = Field(default_factory=list)


class AgentRunner:
    """Execute an approved agent spec through an iterative tool loop."""

    def __init__(
        self,
        model_gateway: RunnerModelGateway,
        prompt_library: PromptLibrary,
        shell_executor: ShellExecutor | None = None,
        file_executor: FileExecutor | None = None,
        notifier: AgentNotifier | None = None,
        scheduler: GoalScheduler | None = None,
        max_tool_rounds: int = 5,
    ):
        self.model_gateway = model_gateway
        self.prompt_library = prompt_library
        self.shell_executor = shell_executor
        self.file_executor = file_executor
        self.notifier = notifier
        self.scheduler = scheduler
        self.max_tool_rounds = max(1, max_tool_rounds)

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

        transcript: list[tuple[AgentToolPlan, list[AgentToolResult]]] = []
        all_tool_results: list[AgentToolResult] = []
        previous_batch: list[dict[str, object]] | None = None
        provider_id = spec.provider_id
        model_id = spec.model_id
        prompt = self._execution_prompt(blueprint, brief, user_request)

        for round_index in range(self.max_tool_rounds):
            try:
                response = self.model_gateway.generate(
                    ModelRequest.from_prompt(prompt).model_copy(
                        update={
                            "max_tokens": 768,
                            "metadata": {
                                "agent_id": spec.agent_id,
                                "agent_type": spec.agent_type,
                                "tool_round": round_index,
                                "tool_results": len(all_tool_results),
                            },
                        }
                    ),
                    config=provider_config,
                )
            except Exception as exc:
                stage = (
                    "could not start"
                    if not all_tool_results
                    else "stopped after tool review"
                )
                return AgentExecutionResult(
                    executed=False,
                    response=f"Agent execution {stage}: {exc}",
                    provider_id=provider_id,
                    model_id=model_id,
                    tool_results=all_tool_results,
                    error=type(exc).__name__,
                )
            provider_id = response.provider_id
            model_id = response.model

            plan = self._parse_tool_plan(response.text)
            if not plan.tool_requests:
                final = self._parse_final_answer(plan.response or response.text)
                return AgentExecutionResult(
                    executed=True,
                    response=final.summary or plan.response or response.text,
                    provider_id=provider_id,
                    model_id=model_id,
                    tool_results=all_tool_results,
                    outcome=final.status,
                    open_questions=final.open_questions,
                )

            batch = [request.model_dump() for request in plan.tool_requests]
            if batch == previous_batch:
                # The agent repeated the exact same tool batch; stop looping.
                break
            previous_batch = batch

            tool_results = self._run_tool_requests(spec, plan.tool_requests, user_request)
            all_tool_results.extend(tool_results)
            transcript.append((plan, tool_results))
            rounds_left = self.max_tool_rounds - round_index - 1
            if rounds_left == 0:
                break
            prompt = self._tool_followup_prompt(
                blueprint,
                brief,
                user_request,
                transcript,
                rounds_left=rounds_left,
            )

        final_prompt = self._final_answer_prompt(blueprint, brief, user_request, transcript)
        try:
            final_response = self.model_gateway.generate(
                ModelRequest.from_prompt(final_prompt).model_copy(
                    update={
                        "max_tokens": 768,
                        "metadata": {
                            "agent_id": spec.agent_id,
                            "agent_type": spec.agent_type,
                            "tool_results": len(all_tool_results),
                        },
                    }
                ),
                config=provider_config,
            )
        except Exception as exc:
            return AgentExecutionResult(
                executed=False,
                response=f"Agent execution stopped after tool review: {exc}",
                provider_id=provider_id,
                model_id=model_id,
                tool_results=all_tool_results,
                error=type(exc).__name__,
            )

        final = self._parse_final_answer(final_response.text)
        return AgentExecutionResult(
            executed=True,
            response=final.summary or final_response.text,
            provider_id=final_response.provider_id,
            model_id=final_response.model,
            tool_results=all_tool_results,
            outcome=final.status,
            open_questions=final.open_questions,
        )

    def _tool_instructions(self) -> str:
        return (
            "You work in tool rounds. In each round you may request tools, see their "
            "results, then request more tools or finish.\n\n"
            "If you need a shell command, return exactly one JSON object in this shape:\n"
            '{"response":"why the command is useful","tool_requests":'
            '[{"kind":"shell","command":"git status -sb","purpose":"Check state"}]}\n\n'
            "For safe file reads use:\n"
            '{"response":"why the file is needed","tool_requests":'
            '[{"kind":"file_read","path":"README.md","purpose":"Read project docs"}]}\n\n'
            "For file writes use `file_write` with `path`, `content`, and `purpose`. "
            "File writes may require user approval.\n\n"
            "To send the user a desktop notification right now use:\n"
            '{"response":"why","tool_requests":'
            '[{"kind":"notify","message":"Build finished","purpose":"Tell the user"}]}\n\n'
            "To set up a recurring goal that The Operator keeps running on a schedule use "
            "`schedule` with `interval_minutes` plus either `message` (recurring desktop "
            "notification) or `mission` (recurring agent task):\n"
            '{"response":"why","tool_requests":'
            '[{"kind":"schedule","mission":"Check disk space and tidy temp files",'
            '"interval_minutes":60,"purpose":"User asked for an hourly cleanup"}]}\n\n'
            "Only request tools that fit the blueprint. Do not request commands for "
            "secrets.\n\n"
            "When you are giving your final answer (no tool needed), return exactly one "
            "JSON object in this shape:\n"
            '{"status":"completed","summary":"the user-facing answer",'
            '"open_questions":[]}\n'
            "Use `\"needs_input\"` (and list the unresolved decisions in `open_questions`) "
            "when you cannot finish without more information from the user. Use "
            "`\"blocked\"` when a guardrail or missing permission stops you. Only use "
            "`\"completed\"` when the task is actually done."
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
            f"{self._tool_instructions()}"
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
        user_request: str,
    ) -> list[AgentToolResult]:
        results: list[AgentToolResult] = []
        for request in requests:
            if request.kind == "shell":
                results.append(self._run_shell_request(spec, request))
            if request.kind == "file_read":
                results.append(self._run_file_read_request(spec, request))
            if request.kind == "file_write":
                results.append(self._run_file_write_request(spec, request))
            if request.kind == "notify":
                results.append(self._run_notify_request(spec, request))
            if request.kind == "schedule":
                results.append(self._run_schedule_request(spec, request, user_request))
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

    def _run_notify_request(
        self,
        spec: AgentSpec,
        request: AgentToolRequest,
    ) -> OperatorToolResult:
        target = " ".join(request.message.split())[:240]
        if "notify_desktop" not in spec.tools_allowed:
            return OperatorToolResult(
                operation="notify",
                target=target,
                purpose=request.purpose,
                reason="This agent spec does not allow notify_desktop.",
            )
        if self.notifier is None:
            return OperatorToolResult(
                operation="notify",
                target=target,
                purpose=request.purpose,
                decision="unavailable",
                reason="Desktop notifications are not attached in this runtime.",
            )
        if not target:
            return OperatorToolResult(
                operation="notify",
                purpose=request.purpose,
                reason="Provide a `message` for the notification.",
            )
        result = self.notifier.send("The Matrix", target)
        return OperatorToolResult(
            operation="notify",
            target=target,
            purpose=request.purpose,
            decision="executed" if result.ok else "failed",
            executed=result.ok,
            reason=result.message,
        )

    def _run_schedule_request(
        self,
        spec: AgentSpec,
        request: AgentToolRequest,
        user_request: str,
    ) -> OperatorToolResult:
        mission = " ".join(request.mission.split())[:600]
        message = " ".join(request.message.split())[:240]
        target = mission or message
        if "operator_schedule" not in spec.tools_allowed:
            return OperatorToolResult(
                operation="schedule",
                target=target,
                purpose=request.purpose,
                reason="This agent spec does not allow operator_schedule.",
            )
        if self.scheduler is None:
            return OperatorToolResult(
                operation="schedule",
                target=target,
                purpose=request.purpose,
                decision="unavailable",
                reason="The Operator scheduler is not attached in this runtime.",
            )
        if request.interval_minutes < 1:
            return OperatorToolResult(
                operation="schedule",
                target=target,
                purpose=request.purpose,
                reason="Provide `interval_minutes` of 1 or more.",
            )
        if not target:
            return OperatorToolResult(
                operation="schedule",
                purpose=request.purpose,
                reason="Provide a `mission` (recurring task) or `message` (notification).",
            )
        try:
            if mission:
                goal = self.scheduler.create_recurring_mission_goal(
                    original_request=user_request,
                    mission_request=mission,
                    interval_minutes=request.interval_minutes,
                )
            else:
                goal = self.scheduler.create_recurring_notification_goal(
                    original_request=user_request,
                    message=message,
                    interval_minutes=request.interval_minutes,
                )
        except Exception as exc:
            return OperatorToolResult(
                operation="schedule",
                target=target,
                purpose=request.purpose,
                decision="failed",
                reason=f"Scheduling failed: {exc}",
            )
        goal_id = str(getattr(goal, "goal_id", ""))
        return OperatorToolResult(
            operation="schedule",
            target=f"every {request.interval_minutes} min: {target}"[:240],
            purpose=request.purpose,
            decision="executed",
            executed=True,
            reason="Recurring Operator goal created and scheduled.",
            goal_id=goal_id or None,
        )

    def _transcript_sections(
        self,
        transcript: list[tuple[AgentToolPlan, list[AgentToolResult]]],
    ) -> str:
        sections: list[str] = []
        for index, (plan, results) in enumerate(transcript, start=1):
            result_json = json.dumps(
                [result.model_dump() for result in results],
                indent=2,
                default=str,
            )
            sections.append(
                f"## Tool round {index}\n\n"
                f"Your tool request summary:\n{plan.response}\n\n"
                f"Tool results:\n{result_json}"
            )
        return "\n\n".join(sections) if sections else "No tools were run."

    def _tool_followup_prompt(
        self,
        blueprint: str,
        brief: OracleBrief,
        user_request: str,
        transcript: list[tuple[AgentToolPlan, list[AgentToolResult]]],
        rounds_left: int,
    ) -> str:
        return (
            f"{blueprint}\n\n"
            "# Current Mission\n\n"
            f"User request:\n{user_request.strip()}\n\n"
            "Oracle brief:\n"
            f"{brief.model_dump_json(indent=2)}\n\n"
            f"{self._transcript_sections(transcript)}\n\n"
            f"You may request more tools for up to {rounds_left} more round(s), or give "
            "your final answer now.\n\n"
            f"{self._tool_instructions()}"
        )

    def _final_answer_prompt(
        self,
        blueprint: str,
        brief: OracleBrief,
        user_request: str,
        transcript: list[tuple[AgentToolPlan, list[AgentToolResult]]],
    ) -> str:
        return (
            f"{blueprint}\n\n"
            "# Current Mission\n\n"
            f"User request:\n{user_request.strip()}\n\n"
            "Oracle brief:\n"
            f"{brief.model_dump_json(indent=2)}\n\n"
            f"{self._transcript_sections(transcript)}\n\n"
            "Now give the final answer as exactly one JSON object in this shape:\n"
            '{"status":"completed","summary":"the user-facing answer","open_questions":[]}\n'
            "Use `\"needs_input\"` with `open_questions` when you still need decisions from "
            "the user, or `\"blocked\"` when a guardrail or approval-needed command stopped "
            "you. Mention blocked or approval-needed commands plainly in the summary if "
            "they affected the result."
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
