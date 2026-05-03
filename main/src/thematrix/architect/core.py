from __future__ import annotations

import hashlib
import json
import re
from typing import Protocol

from thematrix.memory.store import RuntimeStore
from pydantic import BaseModel, Field

from thematrix.prompts import PromptLibrary
from thematrix.prompts.json_tools import extract_json_object
from thematrix.schemas import (
    AgentSpec,
    ModelRequest,
    ModelResponse,
    OracleBrief,
    PrivacyMode,
    ProviderConfig,
    RiskLevel,
)


class ArchitectModelGateway(Protocol):
    def generate(self, request: ModelRequest) -> ModelResponse: ...


class ArchitectDraft(BaseModel):
    agent_type: str
    purpose: str
    capabilities: list[str] = Field(default_factory=list)
    tools_allowed: list[str] = Field(default_factory=list)
    memory_scope: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    interaction_points: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    reusable: bool = True


class Architect:
    """Precise agent designer, memory coordinator, and reuse planner."""

    def __init__(
        self,
        store: RuntimeStore,
        model_gateway: ArchitectModelGateway | None = None,
        prompt_library: PromptLibrary | None = None,
    ):
        self.store = store
        self.model_gateway = model_gateway
        self.prompt_library = prompt_library or PromptLibrary()
        self.last_design_source = "heuristic"

    def design_agent(
        self,
        brief: OracleBrief,
        privacy_mode: PrivacyMode = PrivacyMode.ASK_EACH_TIME,
        provider_config: ProviderConfig | None = None,
    ) -> AgentSpec:
        draft = self._draft_agent(brief, privacy_mode, provider_config)
        agent_type = self._normalize_agent_type(draft.agent_type, brief.intent)
        purpose = self._clean_purpose(
            draft.purpose,
            fallback=self._purpose_for(agent_type, brief.intent),
        )
        candidate = self.store.find_reusable_agent(agent_type=agent_type, purpose=purpose)
        risk = self._max_risk(self._risk_for(brief, agent_type), draft.risk_level)
        agent_id = candidate or self._agent_id(agent_type, purpose)
        prompt_ref = self._prompt_block_ref(agent_id)

        spec = AgentSpec(
            agent_id=agent_id,
            agent_type=agent_type,
            purpose=purpose,
            capabilities=self._capabilities_for(agent_type, draft.capabilities),
            tools_allowed=self._tools_for(agent_type, risk, draft.tools_allowed),
            memory_scope=self._memory_scope_for(draft.memory_scope),
            constraints=self._merge_text_lists(brief.constraints, draft.constraints),
            expected_user_interaction=brief.user_interaction_required,
            interaction_points=self._interaction_points_for(
                draft.interaction_points,
                brief.user_interaction_required,
            ),
            provider_id=provider_config.provider_id if provider_config else "unconfigured",
            model_id=provider_config.selected_model if provider_config else "unconfigured",
            privacy_mode=privacy_mode,
            risk_level=risk,
            reusable=draft.reusable,
            reuse_candidate_id=candidate,
            prompt_block_refs=[prompt_ref],
        )
        blueprint = self._render_agent_blueprint(spec, brief)
        self.prompt_library.write_agent_blueprint(spec.agent_id, blueprint)
        self.store.upsert_agent(spec)
        self.store.record_prompt_block(
            block_ref=prompt_ref,
            block_type="agent_blueprint",
            content=blueprint,
        )
        return spec

    def _draft_agent(
        self,
        brief: OracleBrief,
        privacy_mode: PrivacyMode,
        provider_config: ProviderConfig | None,
    ) -> ArchitectDraft:
        if self.model_gateway is not None:
            try:
                draft = self._draft_with_model(brief, privacy_mode, provider_config)
                self.last_design_source = "model"
                return draft
            except Exception:
                self.last_design_source = "heuristic_fallback"
                return self._draft_with_heuristics(brief)
        self.last_design_source = "heuristic"
        return self._draft_with_heuristics(brief)

    def _draft_with_model(
        self,
        brief: OracleBrief,
        privacy_mode: PrivacyMode,
        provider_config: ProviderConfig | None,
    ) -> ArchitectDraft:
        runtime_context = {
            "privacy_mode": privacy_mode.value,
            "provider_id": provider_config.provider_id if provider_config else "unconfigured",
            "model_id": provider_config.selected_model if provider_config else "unconfigured",
            "user_interaction_required": brief.user_interaction_required,
        }
        prompt = (
            self.prompt_library.read("architect_design")
            .replace("{{ oracle_brief_json }}", brief.model_dump_json(indent=2))
            .replace("{{ runtime_context_json }}", json.dumps(runtime_context, indent=2))
        )
        response = self.model_gateway.generate(
            ModelRequest.from_prompt(prompt).model_copy(
                update={"temperature": 0.0, "max_tokens": 512}
            )
        )
        return ArchitectDraft.model_validate(extract_json_object(response.text))

    def _draft_with_heuristics(self, brief: OracleBrief) -> ArchitectDraft:
        agent_type = self._classify_agent_type(brief.intent)
        purpose = self._purpose_for(agent_type, brief.intent)
        risk = self._risk_for(brief, agent_type)
        return ArchitectDraft(
            agent_type=agent_type,
            purpose=purpose,
            capabilities=self._capabilities_for(agent_type),
            tools_allowed=self._tools_for(agent_type, risk),
            memory_scope=self._memory_scope_for([]),
            constraints=brief.constraints,
            interaction_points=self._interaction_points_for(
                [],
                brief.user_interaction_required,
            ),
            risk_level=risk,
            reusable=True,
        )

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

    def _capabilities_for(
        self,
        agent_type: str,
        proposed: list[str] | None = None,
    ) -> list[str]:
        defaults = {
            "builder": ["inspect_project", "plan_changes", "edit_files", "run_checks"],
            "researcher": ["structure_questions", "compare_sources", "summarize_findings"],
            "sentinel": ["review_permissions", "scan_prompt_risks", "audit_outputs"],
            "operator": ["clarify_request", "coordinate_task", "summarize_result"],
        }
        cleaned = self._clean_text_items(proposed or [], limit=8)
        return cleaned or defaults[agent_type]

    def _tools_for(
        self,
        agent_type: str,
        risk: RiskLevel,
        proposed: list[str] | None = None,
    ) -> list[str]:
        allowed = {
            "builder": ["file_read", "file_write", "shell_guarded", "memory_read", "provider_call"],
            "researcher": ["memory_read", "provider_call"],
            "sentinel": ["memory_read", "security_policy_read", "provider_call"],
            "operator": ["memory_read", "provider_call"],
        }
        defaults = {
            "builder": ["file_read", "file_write", "shell_guarded"],
            "researcher": ["memory_read", "provider_call"],
            "sentinel": ["memory_read", "security_policy_read"],
            "operator": ["memory_read"],
        }
        proposed_tools = [
            tool
            for tool in self._clean_text_items(proposed or [], limit=8)
            if tool in allowed[agent_type]
        ]
        tools = proposed_tools or defaults[agent_type]
        if risk == RiskLevel.HIGH:
            return [tool for tool in tools if tool != "shell_guarded"]
        return tools

    def _risk_for(self, brief: OracleBrief, agent_type: str) -> RiskLevel:
        if brief.ethical_status.value == "blocked":
            return RiskLevel.HIGH
        if brief.ethical_status.value == "sensitive" or agent_type in {"builder", "sentinel"}:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    def _normalize_agent_type(self, proposed: str, intent: str) -> str:
        lowered = proposed.strip().lower()
        if lowered in {"builder", "researcher", "sentinel", "operator"}:
            return lowered
        return self._classify_agent_type(intent)

    def _clean_purpose(self, proposed: str, fallback: str) -> str:
        purpose = " ".join(proposed.split())
        if not purpose:
            return fallback
        return purpose[:300]

    def _memory_scope_for(self, proposed: list[str]) -> list[str]:
        allowed = [
            "wiki/agents/",
            "wiki/workflows/",
            "wiki/decisions/",
            "wiki/risks/",
            "wiki/users/",
        ]
        selected = [scope for scope in self._clean_text_items(proposed, limit=8) if scope in allowed]
        return selected or allowed[:4]

    def _interaction_points_for(
        self,
        proposed: list[str],
        user_interaction_required: bool,
    ) -> list[str]:
        allowed = {
            "before_sensitive_actions",
            "before_cloud_sensitive_use",
            "before_file_writes",
            "before_shell_commands",
            "when_scope_changes",
            "final_user_handoff",
        }
        points = [
            point for point in self._clean_text_items(proposed, limit=8) if point in allowed
        ]
        if not points:
            points = ["before_sensitive_actions", "before_cloud_sensitive_use"]
        if user_interaction_required and "final_user_handoff" not in points:
            points.append("final_user_handoff")
        return points

    def _merge_text_lists(self, first: list[str], second: list[str]) -> list[str]:
        merged: list[str] = []
        for item in [*first, *second]:
            cleaned = " ".join(str(item).split())
            if cleaned and cleaned not in merged:
                merged.append(cleaned[:240])
        return merged[:12]

    def _clean_text_items(self, values: list[str], limit: int) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            item = " ".join(str(value).split())
            if item and item not in cleaned:
                cleaned.append(item[:120])
            if len(cleaned) >= limit:
                break
        return cleaned

    def _max_risk(self, left: RiskLevel, right: RiskLevel) -> RiskLevel:
        order = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}
        return left if order[left] >= order[right] else right

    def _agent_id(self, agent_type: str, purpose: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", purpose.lower()).strip("-")[:48]
        if not slug:
            slug = "general"
        digest = hashlib.sha256(f"{agent_type}:{purpose}".encode("utf-8")).hexdigest()[:10]
        return f"{agent_type}-{slug}-{digest}"

    def _prompt_block_ref(self, agent_id: str) -> str:
        return f"agent-blueprint-{agent_id}"

    def _render_agent_blueprint(self, spec: AgentSpec, brief: OracleBrief) -> str:
        return (
            f"# Agent Blueprint: {spec.agent_id}\n\n"
            f"You are a `{spec.agent_type}` sub-agent in The Matrix Agent System.\n\n"
            f"## Purpose\n\n{spec.purpose}\n\n"
            f"## Oracle Intent\n\n{brief.intent}\n\n"
            "## Operating Rules\n\n"
            "- Stay inside the stated purpose.\n"
            "- Use only the tools listed in this blueprint.\n"
            "- Do not read or reveal raw secrets.\n"
            "- Ask for user confirmation at the listed interaction points.\n"
            "- Keep final answers clear, simple, and concise.\n\n"
            f"## Capabilities\n\n{self._markdown_list(spec.capabilities)}\n\n"
            f"## Tools Allowed\n\n{self._markdown_list(spec.tools_allowed)}\n\n"
            f"## Memory Scope\n\n{self._markdown_list(spec.memory_scope)}\n\n"
            f"## Constraints\n\n{self._markdown_list(spec.constraints)}\n\n"
            f"## Interaction Points\n\n{self._markdown_list(spec.interaction_points)}\n\n"
            f"## Risk Level\n\n{spec.risk_level.value}\n"
        )

    def _markdown_list(self, values: list[str]) -> str:
        if not values:
            return "- None"
        return "\n".join(f"- {value}" for value in values)
