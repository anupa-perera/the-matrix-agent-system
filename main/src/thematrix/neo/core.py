from __future__ import annotations

from thematrix.schemas import AgentSpec, PrivacyMode, RiskLevel, SecurityReport


class Neo:
    """Protective review layer for specs, tools, and outputs."""

    def review_agent_spec(self, spec: AgentSpec) -> SecurityReport:
        issues: list[str] = []
        required_changes: list[str] = []

        if spec.privacy_mode == PrivacyMode.LOCAL_ONLY and spec.provider_id not in {
            "unconfigured",
            "ollama",
            "lmstudio",
            "llamacpp",
        }:
            issues.append("Local-only privacy mode cannot use a cloud provider.")
            required_changes.append("Select a local provider or ask the user to change privacy mode.")

        if spec.risk_level == RiskLevel.HIGH and "shell_guarded" in spec.tools_allowed:
            issues.append("High-risk agents cannot receive shell access in v1.")
            required_changes.append("Remove shell_guarded from the agent tool list.")

        if "secrets_read" in spec.tools_allowed:
            issues.append("Agents cannot read raw secrets.")
            required_changes.append("Route credential-backed actions through Keymaker later.")

        return SecurityReport(
            approved=not issues,
            risk_level=spec.risk_level,
            issues=issues,
            required_changes=required_changes,
        )

    def review_output(self, output: str) -> SecurityReport:
        lowered = output.lower()
        issues: list[str] = []
        if "-----begin private key-----" in lowered:
            issues.append("Output appears to contain private key material.")
        if "ghp_" in output or "sk-" in output:
            issues.append("Output appears to contain a credential-like token.")

        return SecurityReport(
            approved=not issues,
            risk_level=RiskLevel.HIGH if issues else RiskLevel.LOW,
            issues=issues,
            required_changes=["Redact sensitive output."] if issues else [],
        )

