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


class ProviderKind(StrEnum):
    LOCAL = "local"
    CLOUD = "cloud"


class AuthMode(StrEnum):
    NONE = "none"
    API_KEY = "api_key"
    OAUTH = "oauth"
    LOCAL_TOKEN = "local_token"


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
    auth_modes: list[AuthMode]
    supports_model_selection: bool = True
    supports_tools: bool = False
    supports_structured_output: bool = False
    supports_prompt_cache: bool = False
    supports_provider_routing: bool = False
    data_boundary: str
    setup_hint: str


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

