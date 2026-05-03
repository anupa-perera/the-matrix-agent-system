from __future__ import annotations

from thematrix.architect import Architect
from thematrix.memory import MemoryVault, RuntimeStore
from thematrix.neo import Neo
from thematrix.oracle import Oracle
from thematrix.schemas import MatrixRunResult, PrivacyMode, ProviderConfig


class Nebuchadnezzar:
    """Runtime mission flow for one user request."""

    def __init__(
        self,
        oracle: Oracle,
        architect: Architect,
        neo: Neo,
        vault: MemoryVault,
        store: RuntimeStore,
    ):
        self.oracle = oracle
        self.architect = architect
        self.neo = neo
        self.vault = vault
        self.store = store

    def run(
        self,
        user_request: str,
        privacy_mode: PrivacyMode,
        provider_config: ProviderConfig | None = None,
    ) -> MatrixRunResult:
        brief = self.oracle.assess(user_request)
        spec = self.architect.design_agent(
            brief,
            privacy_mode=privacy_mode,
            provider_config=provider_config,
        )
        human_layer = self.oracle.shape_human_layer(brief, spec)
        preflight = self.neo.review_agent_spec(spec)

        if preflight.approved:
            selection = "reused" if spec.reuse_candidate_id else "created"
            if spec.provider_id == "unconfigured":
                provider_text = "No model provider is configured yet."
            else:
                provider_text = (
                    f"Provider `{spec.provider_id}` with model `{spec.model_id}` is configured."
                )
            response = (
                f"Architect {selection} `{spec.agent_id}` as a `{spec.agent_type}` agent. "
                f"{provider_text} "
                f"Oracle shaped it as a {human_layer.temperament}. "
                "Neo approved the preflight review. Runtime execution will be added next."
            )
        else:
            response = "Neo blocked the agent before execution."

        output_report = self.neo.review_output(response)
        result = MatrixRunResult(
            request=user_request,
            oracle_brief=brief,
            agent_spec=spec,
            human_layer=human_layer,
            preflight_report=preflight,
            output_report=output_report,
            response=response,
            metadata={
                "runtime": "nebuchadnezzar",
                "oracle_assessment_source": getattr(
                    self.oracle,
                    "last_assessment_source",
                    "unknown",
                ),
                "architect_design_source": getattr(
                    self.architect,
                    "last_design_source",
                    "unknown",
                ),
            },
        )
        self.store.record_run(result)
        if result.agent_spec is not None:
            self.vault.record_agent_spec(result.agent_spec)
        if result.preflight_report is not None:
            self.vault.record_security_review(result.run_id, "preflight", result.preflight_report)
        if result.output_report is not None:
            self.vault.record_security_review(result.run_id, "output", result.output_report)
        self.vault.record_run(result)
        return result
