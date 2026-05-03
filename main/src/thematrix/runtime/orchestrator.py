from __future__ import annotations

from thematrix.architect import Architect
from thematrix.memory import MemoryVault, RuntimeStore
from thematrix.neo import Neo
from thematrix.oracle import Oracle
from thematrix.schemas import MatrixRunResult, PrivacyMode


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

    def run(self, user_request: str, privacy_mode: PrivacyMode) -> MatrixRunResult:
        brief = self.oracle.assess(user_request)
        spec = self.architect.design_agent(brief, privacy_mode=privacy_mode)
        human_layer = self.oracle.shape_human_layer(brief, spec)
        preflight = self.neo.review_agent_spec(spec)

        if preflight.approved:
            response = (
                f"Architect selected `{spec.agent_id}` as a `{spec.agent_type}` agent. "
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
            metadata={"runtime": "nebuchadnezzar"},
        )
        self.store.record_run(result)
        self.vault.record_run(result)
        return result

