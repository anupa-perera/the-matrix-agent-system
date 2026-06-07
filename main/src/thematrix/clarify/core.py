from __future__ import annotations

from typing import Protocol

from thematrix.memory import RuntimeStore
from thematrix.schemas import (
    AgentSpec,
    ClarificationResponse,
    ClarificationTurn,
    ModelMessage,
    ModelRequest,
    ModelResponse,
)


class ClarificationError(ValueError):
    """Raised when a clarification target cannot be resolved."""


class ClarificationModelGateway(Protocol):
    def generate(self, request: ModelRequest) -> ModelResponse: ...


class ClarificationService:
    """Read-only clarification layer for shaping a request before execution."""

    def __init__(self, store: RuntimeStore, model_gateway: ClarificationModelGateway):
        self.store = store
        self.model_gateway = model_gateway

    def answer(
        self,
        *,
        draft: str,
        question: str,
        target: str = "auto",
        transcript: list[ClarificationTurn] | None = None,
    ) -> ClarificationResponse:
        target = _normalize_target(target)
        if not question.strip():
            raise ClarificationError("Ask a clarification question first.")
        system = self._role_frame(target)
        prompt = self._clarification_prompt(
            draft=draft,
            question=question,
            transcript=transcript or [],
        )
        response = self.model_gateway.generate(
            ModelRequest(
                messages=[
                    ModelMessage(role="system", content=system),
                    ModelMessage(role="user", content=prompt),
                ],
                temperature=0.2,
                max_tokens=700,
                metadata={"purpose": "clarification", "target": target},
            )
        )
        return ClarificationResponse(target=target, answer=response.text.strip())

    def ask_next(
        self,
        *,
        draft: str,
        target: str = "auto",
        transcript: list[ClarificationTurn] | None = None,
    ) -> ClarificationResponse:
        target = _normalize_target(target)
        system = self._role_frame(target)
        prompt = self._next_question_prompt(draft=draft, transcript=transcript or [])
        response = self.model_gateway.generate(
            ModelRequest(
                messages=[
                    ModelMessage(role="system", content=system),
                    ModelMessage(role="user", content=prompt),
                ],
                temperature=0.2,
                max_tokens=300,
                metadata={"purpose": "clarification_question", "target": target},
            )
        )
        answer = response.text.strip() or deterministic_next_question(draft, transcript or [])
        return ClarificationResponse(target=target, answer=answer)

    def summarize(
        self,
        *,
        draft: str,
        transcript: list[ClarificationTurn],
    ) -> str:
        if not transcript:
            return draft.strip()
        try:
            response = self.model_gateway.generate(
                ModelRequest(
                    messages=[
                        ModelMessage(
                            role="system",
                            content=(
                                "You are The Matrix mission brief composer. Convert the user's "
                                "draft and clarification transcript into one clean mission brief. "
                                "Keep concrete constraints, success criteria, safety boundaries, "
                                "and open questions that must be resolved during the mission. Do "
                                "not invent facts. Do not execute tools."
                            ),
                        ),
                        ModelMessage(
                            role="user",
                            content=(
                                f"Original draft:\n{draft.strip() or '(empty)'}\n\n"
                                f"Clarification transcript:\n{_format_transcript(transcript)}\n\n"
                                "Return only the mission brief the runtime should receive."
                            ),
                        ),
                    ],
                    temperature=0.1,
                    max_tokens=900,
                    metadata={"purpose": "clarification_summary"},
                )
            )
        except Exception:
            return deterministic_summary(draft, transcript)
        summary = response.text.strip()
        return summary or deterministic_summary(draft, transcript)

    def _role_frame(self, target: str) -> str:
        if target == "auto":
            return (
                "You are The Matrix. Answer as a read-only clarification assistant. "
                "Choose the best lens for the question: Oracle for intent, Architect "
                "for design, Neo for risk, or The Operator for goals and recurrence. "
                "You cannot run tools, write files, schedule work, or launch missions."
            )
        if target == "matrix":
            return (
                "You are The Matrix, a system-level clarification assistant. Explain "
                "what the user should ask and what the system can safely do. You cannot "
                "run tools, write files, schedule work, or launch missions."
            )
        if target == "oracle":
            return (
                "You are Oracle. Clarify the user's intent, human need, ambiguity, "
                "success criteria, and useful questions. Answer like a thoughtful "
                "human guide: warm, direct, plain-spoken, and practical. Avoid stiff "
                "machine-like phrasing. You cannot run tools, write files, schedule "
                "work, or launch missions."
            )
        if target == "architect":
            return (
                "You are Architect. Clarify system design, decomposition, reusable "
                "agent fit, data flow, and implementation tradeoffs. You cannot run "
                "tools, write files, schedule work, or launch missions."
            )
        if target == "neo":
            return (
                "You are Neo. Clarify risk, permissions, privacy, safety boundaries, "
                "and failure modes. You cannot run tools, write files, schedule work, "
                "or launch missions."
            )
        if target.startswith("agent:"):
            agent_id = target.split(":", 1)[1].strip()
            spec = self.store.get_agent(agent_id)
            if spec is None:
                raise ClarificationError(f"No reusable agent exists with id `{agent_id}`.")
            return self._agent_frame(spec)
        raise ClarificationError(
            "Choose one of: auto, matrix, oracle, architect, neo, or agent:<agent_id>."
        )

    def _agent_frame(self, spec: AgentSpec) -> str:
        return (
            f"You are reusable agent `{spec.agent_id}` in a read-only clarification mode.\n"
            f"Purpose: {spec.purpose}\n"
            f"Capabilities: {_list_text(spec.capabilities)}\n"
            f"Constraints: {_list_text(spec.constraints)}\n"
            f"Allowed tools when a real mission runs: {_list_text(spec.tools_allowed)}\n"
            "For this clarification turn, you cannot use those tools, write files, "
            "schedule work, or launch missions. Answer only to help the user shape "
            "a better mission brief for this agent."
        )

    def _clarification_prompt(
        self,
        *,
        draft: str,
        question: str,
        transcript: list[ClarificationTurn],
    ) -> str:
        return (
            f"Mission draft:\n{draft.strip() or '(empty)'}\n\n"
            f"Transcript so far:\n{_format_transcript(transcript) or '(none)'}\n\n"
            f"User clarification question:\n{question.strip()}\n\n"
            "Answer clearly and briefly. If the request is too vague or risky, say "
            "what must be clarified before running. Do not claim you performed any action."
        )

    def _next_question_prompt(
        self,
        *,
        draft: str,
        transcript: list[ClarificationTurn],
    ) -> str:
        return (
            "Review this mission draft and the intent transcript.\n\n"
            f"Mission draft:\n{draft.strip() or '(empty)'}\n\n"
            f"Intent transcript:\n{_format_transcript(transcript) or '(none)'}\n\n"
            "If the request is clear enough for a local agent mission to start, return "
            "exactly READY. Otherwise ask the user exactly one concise question that "
            "would clarify the highest-impact missing detail before the system builds "
            "or runs the agent. Prefer questions about goal, scope, expected output, "
            "data sources, permissions, schedule, or safety boundaries. Do not answer "
            "the mission. Do not claim you performed any action."
        )


def deterministic_summary(draft: str, transcript: list[ClarificationTurn]) -> str:
    lines = [draft.strip() or "(empty request)"]
    resolved = _resolved_details(transcript)
    if not resolved and transcript:
        resolved = [f"- {turn.content.strip()}" for turn in transcript if turn.content.strip()]
    if resolved:
        lines.append("")
        lines.append("Resolved details:")
        lines.extend(resolved)
    return "\n".join(lines)


def _resolved_details(transcript: list[ClarificationTurn]) -> list[str]:
    details: list[str] = []
    pending_question: str | None = None
    for turn in transcript:
        if turn.kind == "system_question":
            pending_question = turn.content.strip()
        elif turn.kind == "user_answer" and pending_question:
            details.append(f"- {pending_question} -> {turn.content.strip()}")
            pending_question = None
        elif turn.kind == "user_question":
            pending_question = turn.content.strip()
        elif turn.kind == "assistant_answer" and pending_question:
            details.append(f"- {pending_question} -> {turn.content.strip()}")
            pending_question = None
    return details


def deterministic_next_question(draft: str, transcript: list[ClarificationTurn]) -> str:
    if not draft.strip():
        return "What do you want the agent or mission to do?"
    if transcript and transcript[-1].role.value == "user":
        return "READY"
    return "What outcome should this agent produce, and what boundaries should it follow?"


def _format_transcript(turns: list[ClarificationTurn]) -> str:
    lines = []
    for turn in turns:
        label = _turn_label(turn)
        target = f" [{turn.target}]" if turn.target else ""
        lines.append(f"{label}{target}: {turn.content.strip()}")
    return "\n".join(lines)


def _turn_label(turn: ClarificationTurn) -> str:
    if turn.kind == "system_question":
        return "Matrix question"
    if turn.kind == "user_answer":
        return "User answer"
    if turn.kind == "user_question":
        return "User question"
    if turn.kind == "assistant_answer":
        return "Matrix answer"
    return "User" if turn.role.value == "user" else "Matrix"


def _normalize_target(target: str) -> str:
    value = (target or "auto").strip().lower()
    return value or "auto"


def _list_text(values: list[str]) -> str:
    return ", ".join(values) if values else "none"
