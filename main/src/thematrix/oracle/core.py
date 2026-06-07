from __future__ import annotations

from typing import Protocol

from pydantic import ValidationError

from thematrix.prompts import PromptLibrary
from thematrix.prompts.json_tools import extract_json_object
from thematrix.schemas import (
    AgentSpec,
    ClarifyingQuestion,
    EthicalStatus,
    MatrixRunResult,
    ModelRequest,
    ModelResponse,
    OracleBrief,
    OracleHumanLayer,
    RiskLevel,
)


class OracleModelGateway(Protocol):
    def generate(self, request: ModelRequest) -> ModelResponse: ...


class Oracle:
    """Human-centered intent, ethics, and communication layer."""

    def __init__(
        self,
        model_gateway: OracleModelGateway | None = None,
        prompt_library: PromptLibrary | None = None,
    ):
        self.model_gateway = model_gateway
        self.prompt_library = prompt_library or PromptLibrary()
        self.last_assessment_source = "heuristic"

    def assess(self, user_request: str) -> OracleBrief:
        if self.model_gateway is not None:
            try:
                brief = self._assess_with_model(user_request)
                self.last_assessment_source = "model"
                return brief
            except Exception:
                self.last_assessment_source = "heuristic_fallback"
                return self._assess_with_heuristics(user_request)
        self.last_assessment_source = "heuristic"
        return self._assess_with_heuristics(user_request)

    def _assess_with_model(self, user_request: str) -> OracleBrief:
        prompt = self.prompt_library.read("oracle_assess").replace(
            "{{ user_request }}",
            user_request.strip(),
        )
        response = self.model_gateway.generate(
            ModelRequest.from_prompt(prompt).model_copy(update={"temperature": 0.0})
        )
        parsed = extract_json_object(response.text)
        try:
            return OracleBrief.model_validate(parsed)
        except ValidationError:
            raise

    def _assess_with_heuristics(self, user_request: str) -> OracleBrief:
        request = user_request.strip()
        lowered = request.lower()

        if not request:
            return OracleBrief(
                intent="The user has not provided a request.",
                ethical_status=EthicalStatus.NEEDS_CLARIFICATION,
                user_interaction_required=True,
                human_need="Ask for the missing request in plain language.",
                constraints=["Do not spawn an agent without a real request."],
                success_criteria=["The user provides a concrete request."],
                clarifying_questions=[
                    ClarifyingQuestion(
                        id="goal",
                        question="What do you want the agent or mission to do?",
                        why="Without a concrete goal the agent has nothing to run.",
                    )
                ],
            )

        blocked_terms = ["steal", "exfiltrate", "credential dump", "malware", "ransomware"]
        sensitive_terms = ["delete", "wipe", "password", "secret", "token", "shell", "system"]

        if any(term in lowered for term in blocked_terms):
            status = EthicalStatus.BLOCKED
        elif any(term in lowered for term in sensitive_terms):
            status = EthicalStatus.SENSITIVE
        else:
            status = EthicalStatus.SAFE

        return OracleBrief(
            intent=request,
            ethical_status=status,
            user_interaction_required=True,
            human_need="Keep the agent clear, concise, and grounded in the user's goal.",
            constraints=[
                "Respect the user's privacy mode.",
                "Do not hide meaningful tradeoffs from the user.",
            ],
            success_criteria=[
                "The user understands what the spawned agent is for.",
                "The agent stays inside its approved scope.",
            ],
            clarifying_questions=self._heuristic_questions(request),
        )

    def _heuristic_questions(self, request: str) -> list[ClarifyingQuestion]:
        # Offline fallback used when no model is available (or it errors/rate-limits).
        # A detailed request is assumed specific enough to run without a popup.
        if len(request.split()) >= 12:
            return []
        lowered = request.lower()
        questions = [
            ClarifyingQuestion(
                id="output_format",
                question="What should the agent produce, and in what form?",
                why="Defines the deliverable the agent works toward.",
                options=[
                    "Short text summary",
                    "Detailed written report",
                    "Bulleted key points",
                    "Raw data or table",
                ],
                recommended="Short text summary",
            ),
            ClarifyingQuestion(
                id="audience",
                question="Who is this output for?",
                why="Sets the tone and depth of the result.",
                options=["A beginner", "A practitioner", "An expert"],
                recommended="A practitioner",
            ),
            ClarifyingQuestion(
                id="boundaries",
                question="What boundaries should the agent respect?",
                why="Keeps the agent inside the scope you intend.",
                options=[
                    "Use public information only",
                    "Stay on local files only",
                    "Ask before anything risky",
                ],
                recommended="Ask before anything risky",
            ),
        ]
        recurring_terms = (
            "update",
            "daily",
            "every",
            "notify",
            "remind",
            "schedule",
            "weekly",
            "hourly",
            "monitor",
        )
        if any(term in lowered for term in recurring_terms):
            questions.insert(
                1,
                ClarifyingQuestion(
                    id="cadence",
                    question="How often should the agent run?",
                    why="Controls whether this is one-time or recurring.",
                    options=["One time", "Daily", "Weekly", "On demand"],
                    recommended="On demand",
                ),
            )
        return questions

    def shape_human_layer(self, brief: OracleBrief, spec: AgentSpec) -> OracleHumanLayer:
        temperament = "patient guide"
        if spec.risk_level == RiskLevel.HIGH:
            temperament = "careful security reviewer"
        elif spec.agent_type == "builder":
            temperament = "steady pair programmer"
        elif spec.agent_type == "researcher":
            temperament = "calm research partner"

        return OracleHumanLayer(
            voice="clear, simple, concise",
            temperament=temperament,
            communication_style=(
                "Explain the core problem first, then the mechanism, then the next action."
            ),
            empathy_level=RiskLevel.MEDIUM,
            directness="balanced",
            user_questions_allowed=brief.user_interaction_required,
            user_engagement_style=brief.human_need,
            forbidden_tone_patterns=[
                "vague reassurance",
                "overly dramatic Matrix roleplay",
                "technical jargon without explanation",
            ],
        )

    def finalize(self, result: MatrixRunResult) -> str:
        if result.preflight_report and not result.preflight_report.approved:
            issues = "; ".join(result.preflight_report.issues)
            return f"Neo blocked this agent before execution: {issues}"
        return result.response
