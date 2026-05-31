from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class EthicalStatus(StrEnum):
    SAFE = "safe"
    NEEDS_CLARIFICATION = "needs_clarification"
    SENSITIVE = "sensitive"
    BLOCKED = "blocked"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PrivacyMode(StrEnum):
    ASK_EACH_TIME = "ask_each_time"
    CLOUD_ALLOWED = "cloud_allowed"
    LOCAL_ONLY = "local_only"


class FileChangeConsent(StrEnum):
    ASK_EACH_TIME = "ask_each_time"
    ALLOW_ALWAYS = "allow_always"


class ReasoningEffort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


class ProviderKind(StrEnum):
    LOCAL = "local"
    CLOUD = "cloud"


class ProviderAdapterKind(StrEnum):
    OPENAI_COMPATIBLE = "openai_compatible"
    ANTHROPIC_MESSAGES = "anthropic_messages"
    GEMINI_GENERATE_CONTENT = "gemini_generate_content"
    CODEX_EXEC = "codex_exec"


class AuthMode(StrEnum):
    NONE = "none"
    API_KEY = "api_key"
    OAUTH = "oauth"
    LOCAL_TOKEN = "local_token"
    EXTERNAL = "external"


class OracleBrief(BaseModel):
    intent: str
    ethical_status: EthicalStatus
    user_interaction_required: bool
    human_need: str
    constraints: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)


class AgentSpec(BaseModel):
    agent_id: str
    agent_type: str
    purpose: str
    capabilities: list[str] = Field(default_factory=list)
    tools_allowed: list[str] = Field(default_factory=list)
    memory_scope: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    expected_user_interaction: bool = False
    interaction_points: list[str] = Field(default_factory=list)
    provider_id: str = "unconfigured"
    model_id: str = "unconfigured"
    privacy_mode: PrivacyMode = PrivacyMode.ASK_EACH_TIME
    risk_level: RiskLevel = RiskLevel.LOW
    reusable: bool = True
    reuse_candidate_id: str | None = None
    prompt_block_refs: list[str] = Field(default_factory=list)


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    SKIPPED = "skipped"


class MissionTask(BaseModel):
    task_id: str = Field(default_factory=lambda: str(uuid4()))
    sequence: int
    title: str
    description: str
    agent_spec: AgentSpec
    architect_decision: str = ""
    status: TaskStatus = TaskStatus.PENDING
    result_summary: str = ""
    tool_result_count: int = 0
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MissionPlan(BaseModel):
    mission_id: str = Field(default_factory=lambda: str(uuid4()))
    strategy: str = "sequential"
    tasks: list[MissionTask] = Field(default_factory=list)


class OracleHumanLayer(BaseModel):
    voice: str
    temperament: str
    communication_style: str
    empathy_level: RiskLevel = RiskLevel.MEDIUM
    directness: str = "balanced"
    user_questions_allowed: bool = True
    user_engagement_style: str
    forbidden_tone_patterns: list[str] = Field(default_factory=list)


class SecurityReport(BaseModel):
    approved: bool
    risk_level: RiskLevel
    issues: list[str] = Field(default_factory=list)
    required_changes: list[str] = Field(default_factory=list)


class ProviderProfile(BaseModel):
    provider_id: str
    display_name: str
    kind: ProviderKind
    adapter_kind: ProviderAdapterKind
    auth_modes: list[AuthMode]
    suggested_models: list[str] = Field(default_factory=list)
    default_base_url: str | None = None
    supports_model_selection: bool = True
    supports_tools: bool = False
    supports_structured_output: bool = False
    supports_prompt_cache: bool = False
    supports_provider_routing: bool = False
    supported_reasoning_efforts: list[ReasoningEffort] = Field(default_factory=list)
    data_boundary: str
    setup_hint: str


class ProviderConfig(BaseModel):
    provider_id: str
    selected_model: str
    auth_mode: AuthMode
    secret_ref: str | None = None
    base_url: str | None = None
    reasoning_effort: ReasoningEffort | None = None
    enabled: bool = True
    is_default: bool = True
    file_change_consent: FileChangeConsent = FileChangeConsent.ASK_EACH_TIME
    configured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class OnboardingProfile(BaseModel):
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    default_provider_id: str
    default_model: str
    auth_mode: AuthMode
    base_url: str | None = None
    reasoning_effort: ReasoningEffort | None = None
    privacy_mode: PrivacyMode = PrivacyMode.ASK_EACH_TIME
    file_change_consent: FileChangeConsent = FileChangeConsent.ASK_EACH_TIME
    guarded_shell_enabled: bool = True
    vault_path: str
    secret_configured: bool = False


class ModelMessage(BaseModel):
    role: str
    content: str


class ModelRequest(BaseModel):
    messages: list[ModelMessage]
    temperature: float = 0.2
    max_tokens: int = 256
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_prompt(cls, prompt: str) -> "ModelRequest":
        return cls(messages=[ModelMessage(role="user", content=prompt)])


class ModelResponse(BaseModel):
    provider_id: str
    model: str
    text: str
    raw: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)


class ProviderHealth(BaseModel):
    provider_id: str
    ok: bool
    message: str


class MatrixRunResult(BaseModel):
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    request: str
    oracle_brief: OracleBrief
    agent_spec: AgentSpec | None = None
    human_layer: OracleHumanLayer | None = None
    preflight_report: SecurityReport | None = None
    output_report: SecurityReport | None = None
    response: str
    metadata: dict[str, Any] = Field(default_factory=dict)
