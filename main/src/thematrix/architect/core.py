from __future__ import annotations

import hashlib

from thematrix.memory.store import RuntimeStore
from thematrix.schemas import AgentSpec, OracleBrief, PrivacyMode, RiskLevel


class Architect:
    """Precise agent designer, memory coordinator, and reuse planner."""

    def __init__(self, store: RuntimeStore):
        self.store = store

    def design_agent(
        self,
        brief: OracleBrief,
        privacy_mode: PrivacyMode = PrivacyMode.ASK_EACH_TIME,
    ) -> AgentSpec:
        agent_type = self._classify_agent_type(brief.intent)
        purpose = self._purpose_for(agent_type, brief.intent)
        candidate = self.store.find_reusable_agent(agent_type=agent_type, purpose=purpose)
        risk = self._risk_for(brief, agent_type)
        prompt_ref = self._prompt_block_ref(agent_type, purpose)

        spec = AgentSpec(
            agent_id=candidate or f"{agent_type}-baseline",
            agent_type=agent_type,
            purpose=purpose,
            capabilities=self._capabilities_for(agent_type),
            tools_allowed=self._tools_for(agent_type, risk),
            memory_scope=[
                "wiki/agents/",
                "wiki/workflows/",
                "wiki/decisions/",
                "wiki/risks/",
            ],
            constraints=brief.constraints,
            expected_user_interaction=brief.user_interaction_required,
            interaction_points=["before_sensitive_actions", "before_cloud_sensitive_use"],
            privacy_mode=privacy_mode,
            risk_level=risk,
            reusable=True,
            reuse_candidate_id=candidate,
            prompt_block_refs=[prompt_ref],
        )
        self.store.upsert_agent(spec)
        self.store.record_prompt_block(
            block_ref=prompt_ref,
            block_type="agent_blueprint",
            content=f"{agent_type}:{purpose}",
        )
        return spec

    def _classify_agent_type(self, intent: str) -> str:
        lowered = intent.lower()
        if any(term in lowered for term in ["code", "build", "implement", "fix", "repo"]):
            return "builder"
        if any(term in lowered for term in ["research", "compare", "find", "analyze"]):
            return "researcher"
        if any(term in lowered for term in ["secure", "vulnerability", "threat", "audit"]):
            return "sentinel"
        return "operator"

    def _purpose_for(self, agent_type: str, intent: str) -> str:
        if agent_type == "builder":
            return "Plan and implement scoped local software changes."
        if agent_type == "researcher":
            return "Gather, compare, and summarize information for a user decision."
        if agent_type == "sentinel":
            return "Review plans, tools, and outputs for security weaknesses."
        return f"Handle a general user request: {intent[:120]}"

    def _capabilities_for(self, agent_type: str) -> list[str]:
        return {
            "builder": ["inspect_project", "plan_changes", "edit_files", "run_checks"],
            "researcher": ["structure_questions", "compare_sources", "summarize_findings"],
            "sentinel": ["review_permissions", "scan_prompt_risks", "audit_outputs"],
            "operator": ["clarify_request", "coordinate_task", "summarize_result"],
        }[agent_type]

    def _tools_for(self, agent_type: str, risk: RiskLevel) -> list[str]:
        tools = {
            "builder": ["file_read", "file_write", "shell_guarded"],
            "researcher": ["memory_read", "provider_call"],
            "sentinel": ["memory_read", "security_policy_read"],
            "operator": ["memory_read"],
        }[agent_type]
        if risk == RiskLevel.HIGH:
            return [tool for tool in tools if tool != "shell_guarded"]
        return tools

    def _risk_for(self, brief: OracleBrief, agent_type: str) -> RiskLevel:
        if brief.ethical_status.value == "blocked":
            return RiskLevel.HIGH
        if brief.ethical_status.value == "sensitive" or agent_type in {"builder", "sentinel"}:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    def _prompt_block_ref(self, agent_type: str, purpose: str) -> str:
        digest = hashlib.sha256(f"{agent_type}:{purpose}".encode("utf-8")).hexdigest()[:12]
        return f"agent-blueprint-{agent_type}-{digest}"

