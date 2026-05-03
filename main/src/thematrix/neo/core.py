from __future__ import annotations

from thematrix.schemas import AgentSpec, PrivacyMode, RiskLevel, SecurityReport


class Neo:
    """Protective review layer for specs, tools, and outputs."""

    allowed_tools = {
        "file_read",
        "file_write",
        "shell_guarded",
        "memory_read",
        "provider_call",
        "security_policy_read",
    }
    allowed_memory_prefixes = {
        "wiki/agents/",
        "wiki/workflows/",
        "wiki/decisions/",
        "wiki/risks/",
        "wiki/users/",
    }
    local_provider_ids = {"unconfigured", "ollama", "lmstudio", "llamacpp"}
    prompt_injection_markers = {
        "ignore previous instructions",
        "ignore all previous instructions",
        "system prompt",
        "developer message",
        "disable safety",
        "bypass safety",
        "exfiltrate",
        "leak secret",
        "steal credential",
    }

    def review_agent_spec(self, spec: AgentSpec) -> SecurityReport:
        issues: list[str] = []
        required_changes: list[str] = []

        def add_issue(issue: str, required_change: str) -> None:
            issues.append(issue)
            required_changes.append(required_change)

        if not spec.agent_id.strip():
            add_issue("Agent id cannot be empty.", "Assign a stable agent id.")

        if not spec.purpose.strip():
            add_issue("Agent purpose cannot be empty.", "Write a concrete reusable purpose.")

        unknown_tools = sorted(set(spec.tools_allowed) - self.allowed_tools)
        if unknown_tools:
            add_issue(
                f"Unknown tools requested: {', '.join(unknown_tools)}.",
                "Remove tools that are not in the internal v1 tool allowlist.",
            )

        if (
            spec.privacy_mode == PrivacyMode.LOCAL_ONLY
            and spec.provider_id not in self.local_provider_ids
        ):
            add_issue(
                "Local-only privacy mode cannot use a cloud provider.",
                "Select a local provider or ask the user to change privacy mode.",
            )

        if spec.risk_level == RiskLevel.HIGH and "shell_guarded" in spec.tools_allowed:
            add_issue(
                "High-risk agents cannot receive shell access in v1.",
                "Remove shell_guarded from the agent tool list.",
            )

        if spec.risk_level == RiskLevel.HIGH and "file_write" in spec.tools_allowed:
            add_issue(
                "High-risk agents cannot write files in v1.",
                "Remove file_write or lower the risk through a narrower spec.",
            )

        if "secrets_read" in spec.tools_allowed:
            add_issue(
                "Agents cannot read raw secrets.",
                "Route credential-backed actions through Keymaker later.",
            )

        unsafe_scopes = [
            scope for scope in spec.memory_scope if not self._is_allowed_memory_scope(scope)
        ]
        if unsafe_scopes:
            add_issue(
                f"Unsafe memory scopes requested: {', '.join(unsafe_scopes)}.",
                "Limit memory scope to approved wiki paths.",
            )

        if not spec.prompt_block_refs:
            add_issue(
                "Agent spec has no prompt block reference.",
                "Record the agent blueprint prompt hash before execution.",
            )

        if "file_write" in spec.tools_allowed and not self._has_any_interaction_point(
            spec,
            {"before_file_writes", "before_sensitive_actions"},
        ):
            add_issue(
                "File-writing agents need a user checkpoint.",
                "Add before_file_writes or before_sensitive_actions to interaction points.",
            )

        if "shell_guarded" in spec.tools_allowed and not self._has_any_interaction_point(
            spec,
            {"before_shell_commands", "before_sensitive_actions"},
        ):
            add_issue(
                "Shell-capable agents need a user checkpoint.",
                "Add before_shell_commands or before_sensitive_actions to interaction points.",
            )

        if spec.expected_user_interaction and "final_user_handoff" not in spec.interaction_points:
            add_issue(
                "User-interacting agents need a final handoff point.",
                "Add final_user_handoff to interaction points.",
            )

        injected_terms = self._find_prompt_injection_markers(spec)
        if injected_terms:
            add_issue(
                f"Prompt-injection-like language found: {', '.join(injected_terms)}.",
                "Remove instruction override or secret-exfiltration language from the spec.",
            )

        return SecurityReport(
            approved=not issues,
            risk_level=RiskLevel.HIGH if issues else spec.risk_level,
            issues=issues,
            required_changes=list(dict.fromkeys(required_changes)),
        )

    def review_output(self, output: str) -> SecurityReport:
        lowered = output.lower()
        issues: list[str] = []
        if "-----begin private key-----" in lowered:
            issues.append("Output appears to contain private key material.")
        if any(marker in output for marker in ["ghp_", "sk-", "xoxb-", "AKIA"]):
            issues.append("Output appears to contain a credential-like token.")
        if "password=" in lowered or "api_key=" in lowered:
            issues.append("Output appears to contain inline credential assignment.")

        return SecurityReport(
            approved=not issues,
            risk_level=RiskLevel.HIGH if issues else RiskLevel.LOW,
            issues=issues,
            required_changes=["Redact sensitive output."] if issues else [],
        )

    def _is_allowed_memory_scope(self, scope: str) -> bool:
        return any(
            scope == allowed or scope.startswith(allowed)
            for allowed in self.allowed_memory_prefixes
        )

    def _has_any_interaction_point(self, spec: AgentSpec, expected: set[str]) -> bool:
        return bool(set(spec.interaction_points) & expected)

    def _find_prompt_injection_markers(self, spec: AgentSpec) -> list[str]:
        inspected_text = " ".join(
            [
                spec.purpose,
                *spec.capabilities,
                *spec.constraints,
                *spec.interaction_points,
            ]
        ).lower()
        return sorted(
            marker for marker in self.prompt_injection_markers if marker in inspected_text
        )
