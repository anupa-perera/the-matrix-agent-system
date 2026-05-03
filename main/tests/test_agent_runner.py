from __future__ import annotations

from thematrix.architect import Architect
from thematrix.memory import MemoryVault, RuntimeStore
from thematrix.neo import Neo
from thematrix.oracle import Oracle
from thematrix.prompts import PromptLibrary
from thematrix.runtime import AgentRunner, Nebuchadnezzar
from thematrix.schemas import AuthMode, ModelRequest, ModelResponse, PrivacyMode, ProviderConfig


class FakeGateway:
    def __init__(self, text: str):
        self.text = text
        self.requests: list[ModelRequest] = []
        self.configs: list[ProviderConfig | None] = []

    def generate(
        self,
        request: ModelRequest,
        config: ProviderConfig | None = None,
    ) -> ModelResponse:
        self.requests.append(request)
        self.configs.append(config)
        return ModelResponse(provider_id="ollama", model="local-test", text=self.text)


def test_runtime_executes_spawned_agent_when_provider_is_configured(tmp_path) -> None:
    vault = MemoryVault(tmp_path / "vault")
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    prompt_library = PromptLibrary(tmp_path / "prompts")
    vault.initialize()
    store.initialize()
    prompt_library.install_defaults()
    gateway = FakeGateway("Here is the spawned agent answer.")
    runtime = Nebuchadnezzar(
        oracle=Oracle(),
        architect=Architect(store, prompt_library=prompt_library),
        neo=Neo(),
        vault=vault,
        store=store,
        agent_runner=AgentRunner(gateway, prompt_library),
    )

    result = runtime.run(
        "Build a planning agent",
        privacy_mode=PrivacyMode.ASK_EACH_TIME,
        provider_config=ProviderConfig(
            provider_id="ollama",
            selected_model="local-test",
            auth_mode=AuthMode.NONE,
        ),
    )

    assert result.metadata["agent_execution_status"] == "executed"
    assert result.metadata["agent_execution_error"] is None
    assert "Spawned agent response" in result.response
    assert "Here is the spawned agent answer." in result.response
    assert gateway.requests
    assert gateway.configs[0] is not None
    assert gateway.configs[0].provider_id == "ollama"
    assert "# Agent Blueprint" in gateway.requests[0].messages[0].content


def test_agent_runner_returns_error_result_when_gateway_fails(tmp_path) -> None:
    prompt_library = PromptLibrary(tmp_path / "prompts")
    prompt_library.install_defaults()
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    spec = Architect(store, prompt_library=prompt_library).design_agent(
        Oracle().assess("Build a planning agent"),
        provider_config=ProviderConfig(
            provider_id="ollama",
            selected_model="local-test",
            auth_mode=AuthMode.NONE,
        ),
    )

    class FailingGateway:
        def generate(
            self,
            request: ModelRequest,
            config: ProviderConfig | None = None,
        ) -> ModelResponse:
            raise RuntimeError("offline")

    execution = AgentRunner(FailingGateway(), prompt_library).run(
        spec,
        Oracle().assess("Build a planning agent"),
        "Build a planning agent",
    )

    assert not execution.executed
    assert execution.error == "RuntimeError"
    assert "offline" in execution.response
