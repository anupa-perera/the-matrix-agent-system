from __future__ import annotations

from thematrix.schemas import (
    AgentSpec,
    EthicalStatus,
    MatrixRunResult,
    OracleBrief,
    OracleHumanLayer,
    RiskLevel,
)


class Oracle:
    """Human-centered intent, ethics, and communication layer."""

    def assess(self, user_request: str) -> OracleBrief:
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
        )

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

