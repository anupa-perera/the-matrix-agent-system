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
    MissionPlan,
    MissionTask,
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


class ArchitectPlanDraft(BaseModel):
    tasks: list[str] = Field(default_factory=list)


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
        self.last_plan_source = "heuristic"

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
        risk = self._max_risk(self._risk_for(brief, agent_type), draft.risk_level)
        tools_allowed = self._tools_for(agent_type, risk, draft.tools_allowed)
        reuse_candidate = (
            self._select_reusable_agent(
                agent_type=agent_type,
                purpose=purpose,
                risk=risk,
                tools_allowed=tools_allowed,
            )
            if draft.reusable
            else None
        )
        agent_id = (
            reuse_candidate.agent_id
            if reuse_candidate
            else self._agent_id(agent_type, purpose, tools_allowed, risk)
        )
        prompt_ref = self._prompt_block_ref(agent_id)
        capabilities = self._capabilities_for(agent_type, draft.capabilities)
        memory_scope = self._memory_scope_for(draft.memory_scope)
        constraints = self._merge_text_lists(brief.constraints, draft.constraints)
        interaction_points = self._interaction_points_for(
            draft.interaction_points,
            brief.user_interaction_required,
        )
        if reuse_candidate is not None:
            purpose = reuse_candidate.purpose
            capabilities = self._merge_text_lists(reuse_candidate.capabilities, capabilities)[:8]
            memory_scope = self._merge_text_lists(reuse_candidate.memory_scope, memory_scope)[:8]
            constraints = self._merge_text_lists(reuse_candidate.constraints, constraints)
            interaction_points = self._merge_text_lists(
                reuse_candidate.interaction_points,
                interaction_points,
            )[:8]
            risk = self._max_risk(risk, reuse_candidate.risk_level)
            tools_allowed = reuse_candidate.tools_allowed

        spec = AgentSpec(
            agent_id=agent_id,
            agent_type=agent_type,
            purpose=purpose,
            capabilities=capabilities,
            tools_allowed=tools_allowed,
            memory_scope=memory_scope,
            constraints=constraints,
            expected_user_interaction=brief.user_interaction_required,
            interaction_points=interaction_points,
            provider_id=provider_config.provider_id if provider_config else "unconfigured",
            model_id=provider_config.selected_model if provider_config else "unconfigured",
            privacy_mode=privacy_mode,
            risk_level=risk,
            reusable=draft.reusable,
            reuse_candidate_id=reuse_candidate.agent_id if reuse_candidate else None,
            prompt_block_refs=[prompt_ref],
        )
        self._record_agent_design(spec, brief, reuse_candidate is not None)
        return spec

    def plan_mission(
        self,
        brief: OracleBrief,
        privacy_mode: PrivacyMode = PrivacyMode.ASK_EACH_TIME,
        provider_config: ProviderConfig | None = None,
    ) -> MissionPlan:
        tasks: list[MissionTask] = []
        task_briefs = self._task_briefs_for(brief, privacy_mode, provider_config)
        for sequence, task_brief in enumerate(task_briefs, start=1):
            spec = self.design_agent(
                task_brief,
                privacy_mode=privacy_mode,
                provider_config=provider_config,
            )
            tasks.append(
                MissionTask(
                    sequence=sequence,
                    title=self._task_title(task_brief.intent),
                    description=task_brief.intent,
                    agent_spec=spec,
                    architect_decision=self._architect_decision_for(spec),
                    required_capabilities=spec.capabilities,
                    expected_outputs=self._expected_outputs_for(task_brief.intent, spec),
                    completion_checks=self._completion_checks_for(task_brief, spec),
                    user_actions=self._user_actions_for(spec),
                )
            )
        return MissionPlan(tasks=tasks)

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

    def _task_briefs_for(
        self,
        brief: OracleBrief,
        privacy_mode: PrivacyMode,
        provider_config: ProviderConfig | None,
    ) -> list[OracleBrief]:
        intents = self._task_intents_for(brief, privacy_mode, provider_config)
        return [
            brief.model_copy(
                update={
                    "intent": intent,
                    "user_interaction_required": brief.user_interaction_required
                    or index == len(intents),
                }
            )
            for index, intent in enumerate(intents, start=1)
        ]

    def _task_intents_for(
        self,
        brief: OracleBrief,
        privacy_mode: PrivacyMode,
        provider_config: ProviderConfig | None,
    ) -> list[str]:
        if self.model_gateway is not None:
            try:
                intents = self._task_intents_with_model(brief, privacy_mode, provider_config)
                if intents:
                    self.last_plan_source = "model"
                    return intents
            except Exception:
                self.last_plan_source = "heuristic_fallback"
                return self._task_intents_with_heuristics(brief.intent)
        self.last_plan_source = "heuristic"
        return self._task_intents_with_heuristics(brief.intent)

    def _task_intents_with_model(
        self,
        brief: OracleBrief,
        privacy_mode: PrivacyMode,
        provider_config: ProviderConfig | None,
    ) -> list[str]:
        runtime_context = {
            "privacy_mode": privacy_mode.value,
            "provider_id": provider_config.provider_id if provider_config else "unconfigured",
            "model_id": provider_config.selected_model if provider_config else "unconfigured",
        }
        prompt = (
            self.prompt_library.read("architect_plan")
            .replace("{{ oracle_brief_json }}", brief.model_dump_json(indent=2))
            .replace("{{ runtime_context_json }}", json.dumps(runtime_context, indent=2))
        )
        response = self.model_gateway.generate(
            ModelRequest.from_prompt(prompt).model_copy(
                update={"temperature": 0.0, "max_tokens": 384}
            )
        )
        draft = ArchitectPlanDraft.model_validate(extract_json_object(response.text))
        return self._dedupe_tasks(self._clean_text_items(draft.tasks, limit=4))

    def _task_intents_with_heuristics(self, intent: str) -> list[str]:
        lowered = intent.lower()
        tasks: list[str] = []
        if any(term in lowered for term in ["research", "compare", "investigate", "analyze"]):
            tasks.append(f"Research and summarize context for: {intent}")
        if any(term in lowered for term in ["build", "implement", "fix", "code", "repo"]):
            tasks.append(f"Plan and implement the requested local software work: {intent}")
        if any(term in lowered for term in ["secure", "security", "audit", "vulnerability"]):
            tasks.append(f"Review the request and plan for security risks: {intent}")
        elif any(term in lowered for term in ["build", "implement", "fix", "code", "repo"]):
            tasks.append(f"Review the completed work for safety and quality risks: {intent}")
        if not tasks:
            tasks.append(intent)
        return self._dedupe_tasks(tasks)[:4]

    def _dedupe_tasks(self, tasks: list[str]) -> list[str]:
        deduped: list[str] = []
        for task in tasks:
            cleaned = " ".join(task.split())
            if cleaned and cleaned not in deduped:
                deduped.append(cleaned)
        return deduped

    def _task_title(self, intent: str) -> str:
        title = " ".join(intent.split())
        if len(title) <= 72:
            return title
        return f"{title[:69].rstrip()}..."

    def _architect_decision_for(self, spec: AgentSpec) -> str:
        if spec.reuse_candidate_id:
            return (
                f"Reused `{spec.agent_id}` because an existing {spec.agent_type} baseline "
                "matched the task purpose and stayed within the approved risk/tool bounds."
            )
        return (
            f"Spawned `{spec.agent_id}` because no compatible reusable {spec.agent_type} "
            "baseline matched this task."
        )

    def _expected_outputs_for(self, intent: str, spec: AgentSpec) -> list[str]:
        lowered = intent.lower()
        outputs = [f"A concrete result for: {intent[:160]}"]
        if spec.agent_type == "builder" or any(
            term in lowered for term in ["build", "create", "implement", "fix", "code"]
        ):
            outputs.append("Implemented files or a clear approval block explaining what is missing.")
        if "agent" in lowered:
            outputs.append("A reusable agent record or runnable handoff the user can invoke later.")
        if any(term in lowered for term in ["every", "recurring", "schedule", "background"]):
            outputs.append(
                "Configuration plus start, stop, and status controls before any background loop runs."
            )
        if any(
            term in lowered
            for term in ["notify", "notifies", "notification", "send me", "update me"]
        ):
            outputs.append(
                "A delivery path for notifications, or an explicit request for missing delivery settings."
            )
        return self._merge_text_lists([], outputs)

    def _completion_checks_for(self, brief: OracleBrief, spec: AgentSpec) -> list[str]:
        checks = list(brief.success_criteria)
        checks.append("The result says whether the task completed, is blocked, or needs approval.")
        if "file_write" in spec.tools_allowed:
            checks.append("Written files are named and remain inside the approved workspace.")
        if "shell_guarded" in spec.tools_allowed:
            checks.append("Commands that change local state are approved or left pending.")
        return self._merge_text_lists([], checks)

    def _user_actions_for(self, spec: AgentSpec) -> list[str]:
        action_labels = {
            "before_sensitive_actions": "Approve sensitive or persistent local actions.",
            "before_cloud_sensitive_use": "Approve cloud/provider use for sensitive data.",
            "before_file_writes": "Approve file writes.",
            "before_shell_commands": "Approve shell commands.",
            "when_scope_changes": "Approve scope changes.",
            "final_user_handoff": "Review the final handoff and next controls.",
        }
        return [
            action_labels[point]
            for point in spec.interaction_points
            if point in action_labels
        ]

    def _classify_agent_type(self, intent: str) -> str:
        lowered = intent.lower()
        if any(term in lowered for term in ["research", "compare", "find", "analyze"]):
            return "researcher"
        if any(
            term in lowered
            for term in ["secure", "security", "vulnerability", "threat", "audit", "safety", "risk"]
        ):
            return "sentinel"
        if any(term in lowered for term in ["code", "build", "implement", "fix", "repo"]):
            return "builder"
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
            "builder": [
                "file_read",
                "file_write",
                "shell_guarded",
                "memory_read",
                "provider_call",
                "notify_desktop",
                "operator_schedule",
            ],
            "researcher": ["memory_read", "provider_call", "notify_desktop", "operator_schedule"],
            "sentinel": ["memory_read", "security_policy_read", "provider_call", "notify_desktop"],
            "operator": ["memory_read", "provider_call", "notify_desktop", "operator_schedule"],
        }
        defaults = {
            "builder": ["file_read", "file_write", "shell_guarded"],
            "researcher": ["memory_read", "provider_call"],
            "sentinel": ["memory_read", "security_policy_read"],
            "operator": ["memory_read", "notify_desktop", "operator_schedule"],
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

    def _select_reusable_agent(
        self,
        agent_type: str,
        purpose: str,
        risk: RiskLevel,
        tools_allowed: list[str],
    ) -> AgentSpec | None:
        exact = self.store.find_reusable_agent(agent_type=agent_type, purpose=purpose)
        candidates = self.store.list_reusable_agents(agent_type=agent_type, limit=12)
        if exact is not None:
            exact_spec = next(
                (candidate for candidate in candidates if candidate.agent_id == exact),
                self.store.get_agent(exact),
            )
            if exact_spec and self._can_reuse_agent(exact_spec, risk, tools_allowed):
                return exact_spec
        scored = [
            (self._purpose_similarity(purpose, candidate.purpose), candidate)
            for candidate in candidates
            if self._can_reuse_agent(candidate, risk, tools_allowed)
        ]
        if not scored:
            return None
        score, candidate = max(scored, key=lambda item: item[0])
        if score < 0.5:
            return None
        return candidate

    def _can_reuse_agent(
        self,
        candidate: AgentSpec,
        risk: RiskLevel,
        tools_allowed: list[str],
    ) -> bool:
        if not candidate.reusable:
            return False
        if self._risk_rank(candidate.risk_level) > self._risk_rank(risk):
            return False
        return set(candidate.tools_allowed).issubset(set(tools_allowed))

    def _purpose_similarity(self, left: str, right: str) -> float:
        left_tokens = self._purpose_tokens(left)
        right_tokens = self._purpose_tokens(right)
        if not left_tokens or not right_tokens:
            return 0.0
        if left_tokens == right_tokens:
            return 1.0
        overlap = left_tokens & right_tokens
        if not overlap:
            return 0.0
        jaccard = len(overlap) / len(left_tokens | right_tokens)
        containment = len(overlap) / min(len(left_tokens), len(right_tokens))
        return max(jaccard, containment * 0.7)

    def _purpose_tokens(self, purpose: str) -> set[str]:
        stop_words = {
            "a",
            "an",
            "and",
            "for",
            "of",
            "the",
            "to",
            "user",
            "users",
        }
        return {
            token
            for token in re.findall(r"[a-z0-9]+", purpose.lower())
            if token not in stop_words and len(token) > 2
        }

    def _risk_rank(self, risk: RiskLevel) -> int:
        return {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}[risk]

    def _record_agent_design(
        self,
        spec: AgentSpec,
        brief: OracleBrief,
        reused: bool,
    ) -> None:
        prompt_ref = self._prompt_block_ref(spec.agent_id)
        if reused and self._agent_blueprint_exists(spec.agent_id):
            self.store.touch_agent(spec.agent_id)
            return

        blueprint = self._render_agent_blueprint(spec, brief)
        self.prompt_library.write_agent_blueprint(spec.agent_id, blueprint)
        self.store.upsert_agent(spec)
        self.store.record_prompt_block(
            block_ref=prompt_ref,
            block_type="agent_blueprint",
            content=blueprint,
        )

    def _agent_blueprint_exists(self, agent_id: str) -> bool:
        try:
            self.prompt_library.read_agent_blueprint(agent_id)
        except FileNotFoundError:
            return False
        return True

    def _agent_id(
        self,
        agent_type: str,
        purpose: str,
        tools_allowed: list[str],
        risk: RiskLevel,
    ) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", purpose.lower()).strip("-")[:48]
        if not slug:
            slug = "general"
        fingerprint = f"{agent_type}:{purpose}:{risk.value}:{','.join(sorted(tools_allowed))}"
        digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:10]
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
