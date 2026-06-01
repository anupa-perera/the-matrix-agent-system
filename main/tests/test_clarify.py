import pytest

from thematrix.clarify import ClarificationError, ClarificationService
from thematrix.clarify.core import deterministic_summary
from thematrix.config import MatrixPaths
from thematrix.memory import RuntimeStore
from thematrix.schemas import (
    AgentSpec,
    ClarificationRole,
    ClarificationTurn,
    ModelRequest,
    ModelResponse,
)


class FakeGateway:
    def __init__(self, text: str = "clarified") -> None:
        self.text = text
        self.requests: list[ModelRequest] = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(provider_id="fake", model="fake", text=self.text)


class FailingGateway:
    def generate(self, request: ModelRequest) -> ModelResponse:
        raise RuntimeError("model unavailable")


def _store(tmp_path) -> RuntimeStore:
    paths = MatrixPaths(home=tmp_path / "home", vault=tmp_path / "vault")
    store = RuntimeStore(paths.runtime_db)
    store.initialize()
    return store


def test_clarification_service_frames_matrix_as_read_only(tmp_path) -> None:
    store = _store(tmp_path)
    gateway = FakeGateway("Ask for the exact folder and output format.")
    service = ClarificationService(store, gateway)

    response = service.answer(
        draft="organize my notes",
        question="What should I clarify first?",
        target="auto",
    )

    assert response.answer == "Ask for the exact folder and output format."
    system = gateway.requests[-1].messages[0].content
    assert "You are The Matrix" in system
    assert "cannot run tools" in system
    assert gateway.requests[-1].metadata == {"purpose": "clarification", "target": "auto"}


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("oracle", "You are Oracle"),
        ("architect", "You are Architect"),
        ("neo", "You are Neo"),
        ("matrix", "You are The Matrix"),
    ],
)
def test_clarification_service_uses_target_role_frames(tmp_path, target: str, expected: str) -> None:
    store = _store(tmp_path)
    gateway = FakeGateway()
    service = ClarificationService(store, gateway)

    service.answer(draft="build a helper", question="What matters?", target=target)

    assert expected in gateway.requests[-1].messages[0].content


def test_clarification_service_agent_target_includes_agent_contract(tmp_path) -> None:
    store = _store(tmp_path)
    store.upsert_agent(
        AgentSpec(
            agent_id="researcher",
            agent_type="analyst",
            purpose="Compare AI tools.",
            capabilities=["compare products"],
            constraints=["Use plain English"],
            tools_allowed=["memory_read"],
        )
    )
    gateway = FakeGateway()
    service = ClarificationService(store, gateway)

    service.answer(
        draft="compare tools",
        question="What should this agent ask?",
        target="agent:researcher",
    )

    system = gateway.requests[-1].messages[0].content
    assert "reusable agent `researcher`" in system
    assert "Compare AI tools." in system
    assert "compare products" in system
    assert "Use plain English" in system
    assert "memory_read" in system
    assert "cannot use those tools" in system


def test_clarification_service_unknown_agent_returns_clear_error(tmp_path) -> None:
    store = _store(tmp_path)
    service = ClarificationService(store, FakeGateway())

    with pytest.raises(ClarificationError, match="No reusable agent exists"):
        service.answer(draft="run agent", question="Can it do this?", target="agent:missing")


def test_clarification_summary_falls_back_when_model_fails(tmp_path) -> None:
    store = _store(tmp_path)
    service = ClarificationService(store, FailingGateway())
    turns = [
        ClarificationTurn(
            role=ClarificationRole.USER,
            content="What is missing?",
            target="oracle",
        ),
        ClarificationTurn(
            role=ClarificationRole.ASSISTANT,
            content="Ask for the folder.",
            target="oracle",
        ),
    ]

    summary = service.summarize(draft="organize notes", transcript=turns)

    assert summary == deterministic_summary("organize notes", turns)
    assert "Original request:" in summary
    assert "User [oracle]: What is missing?" in summary
