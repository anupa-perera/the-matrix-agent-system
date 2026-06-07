from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
from inspect import Parameter, signature
import json
from secrets import token_urlsafe
from threading import Event, Lock, Thread, Timer
from urllib.parse import parse_qs, urlencode, urlparse
import webbrowser

from thematrix.clarify import ClarificationError, ClarificationService
from thematrix.config import MatrixPaths
from thematrix.memory import MemoryVault, RuntimeStore
from thematrix.operator import TheOperator
from thematrix.prompts import PromptLibrary
from thematrix.providers import default_model_gateway, detect_local_providers
from thematrix.providers.oauth import (
    OAuthPendingSetup,
    OAuthProviderError,
    build_openrouter_oauth_setup,
    exchange_openrouter_code,
    setup_form_from_oauth,
)
from thematrix.schemas import (
    AgentSpec,
    ClarificationRole,
    ClarificationSession,
    ClarificationTurn,
    ClarifyingQuestion,
    MatrixRunResult,
    MissionTask,
    OperatorGoalKind,
    OperatorGoalStatus,
)
from thematrix.security import Keymaker
from thematrix.ui.dashboard import render_dashboard_html, write_dashboard
from thematrix.ui.matrix_background import (
    matrix_background_canvas,
    matrix_background_styles,
    matrix_rain_script,
)
from thematrix.ui.setup_server import apply_setup_form, render_setup_form

MAX_APP_BODY_BYTES = 64 * 1024
DEFAULT_APP_TIMEOUT_SECONDS = 60 * 60
APP_SESSION_COOKIE_NAME = "thematrix_app_session"
SESSION_COOKIE_ATTRIBUTES = "Path=/; HttpOnly; SameSite=Strict"


@dataclass(frozen=True)
class AppUiResponse:
    result: MatrixRunResult | None = None
    error: str | None = None
    message: str | None = None
    busy: bool = False


@dataclass
class MissionStatusEvent:
    stage: str
    message: str
    details: dict[str, object] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class MissionJob:
    job_id: str
    kind: str
    request: str
    agent_id: str | None = None
    status: str = "queued"
    stage: str = "queued"
    message: str = "Mission accepted. Waiting for the runtime."
    result: MatrixRunResult | None = None
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    started_at: str | None = None
    completed_at: str | None = None
    events: list[MissionStatusEvent] = field(default_factory=list)
    lock: Lock = field(default_factory=Lock)

    def record(self, stage: str, message: str, details: dict[str, object] | None = None) -> None:
        with self.lock:
            self.stage = stage
            self.message = message
            self.events.append(MissionStatusEvent(stage, message, details or {}))

    def start(self) -> None:
        with self.lock:
            self.status = "running"
            self.stage = "starting"
            self.message = "Mission is starting."
            self.started_at = datetime.now(UTC).isoformat()
            self.events.append(MissionStatusEvent("starting", "Mission is starting."))

    def complete(self, result: MatrixRunResult) -> None:
        with self.lock:
            self.status = "completed"
            self.stage = "completed"
            self.message = "Mission completed."
            self.result = result
            self.completed_at = datetime.now(UTC).isoformat()
            self.events.append(
                MissionStatusEvent(
                    "completed",
                    "Mission completed.",
                    {"run_id": result.run_id},
                )
            )

    def fail(self, error: str) -> None:
        with self.lock:
            self.status = "failed"
            self.stage = "failed"
            self.message = "Mission failed."
            self.error = error
            self.completed_at = datetime.now(UTC).isoformat()
            self.events.append(MissionStatusEvent("failed", "Mission failed.", {"error": error}))


class MissionRegistry:
    def __init__(self) -> None:
        self._lock = Lock()
        self._jobs: dict[str, MissionJob] = {}

    def create(self, kind: str, request: str, agent_id: str | None = None) -> MissionJob:
        job = MissionJob(
            job_id=token_urlsafe(12),
            kind=kind,
            request=request,
            agent_id=agent_id,
        )
        job.record("queued", "Mission accepted. Waiting for the runtime.")
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> MissionJob | None:
        with self._lock:
            return self._jobs.get(job_id)


@dataclass
class OracleJob:
    job_id: str
    question: str
    status: str = "running"
    message: str = "Oracle signal acquired."
    answer: str = ""
    error: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str | None = None
    lock: Lock = field(default_factory=Lock)

    def complete(self, answer: str) -> None:
        with self.lock:
            self.status = "completed"
            self.message = "Oracle transmission complete."
            self.answer = answer
            self.completed_at = datetime.now(UTC).isoformat()

    def fail(self, error: str) -> None:
        with self.lock:
            self.status = "failed"
            self.message = "Oracle transmission failed."
            self.error = error
            self.completed_at = datetime.now(UTC).isoformat()


class OracleJobRegistry:
    def __init__(self) -> None:
        self._lock = Lock()
        self._jobs: dict[str, OracleJob] = {}

    def create(self, question: str) -> OracleJob:
        job = OracleJob(job_id=token_urlsafe(12), question=question)
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> OracleJob | None:
        with self._lock:
            return self._jobs.get(job_id)

@dataclass
class ApprovalRequest:
    approval_id: str
    job_id: str
    target: str
    reason: str
    purpose: str
    status: str = "pending"
    approved: bool | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    decided_at: str | None = None
    event: Event = field(default_factory=Event, repr=False)


class ApprovalRegistry:
    def __init__(self, timeout_seconds: int = 15 * 60) -> None:
        self._lock = Lock()
        self._approvals: dict[str, ApprovalRequest] = {}
        self.timeout_seconds = timeout_seconds

    def request(self, job: MissionJob, target: str, reason: str, purpose: str) -> bool:
        approval = ApprovalRequest(
            approval_id=token_urlsafe(12),
            job_id=job.job_id,
            target=target,
            reason=reason,
            purpose=purpose,
        )
        with self._lock:
            self._approvals[approval.approval_id] = approval
        job.record(
            "approval_required",
            "Agent requested user approval.",
            self._payload(approval),
        )
        if not approval.event.wait(self.timeout_seconds):
            with self._lock:
                if approval.status == "pending":
                    approval.status = "timed_out"
                    approval.approved = False
                    approval.decided_at = datetime.now(UTC).isoformat()
                    approval.event.set()
            job.record(
                "approval_timeout",
                "Approval timed out and was denied.",
                self._payload(approval),
            )
            return False
        with self._lock:
            approved = bool(approval.approved)
            payload = self._payload(approval)
        job.record(
            "approval_granted" if approved else "approval_denied",
            "User approved the request." if approved else "User denied the request.",
            payload,
        )
        return approved

    def respond(self, approval_id: str, approved: bool) -> ApprovalRequest | None:
        with self._lock:
            approval = self._approvals.get(approval_id)
            if approval is None or approval.status != "pending":
                return approval
            approval.status = "approved" if approved else "denied"
            approval.approved = approved
            approval.decided_at = datetime.now(UTC).isoformat()
            approval.event.set()
            return approval

    def pending_payloads(self, job_id: str) -> list[dict[str, object]]:
        with self._lock:
            return [
                self._payload(approval)
                for approval in self._approvals.values()
                if approval.job_id == job_id and approval.status == "pending"
            ]

    def pending_all_payloads(self) -> list[dict[str, object]]:
        with self._lock:
            return [
                self._payload(approval)
                for approval in self._approvals.values()
                if approval.status == "pending"
            ]

    def _payload(self, approval: ApprovalRequest) -> dict[str, object]:
        return {
            "approval_id": approval.approval_id,
            "job_id": approval.job_id,
            "target": approval.target,
            "reason": approval.reason,
            "purpose": approval.purpose,
            "status": approval.status,
            "created_at": approval.created_at,
            "decided_at": approval.decided_at,
        }


class ClarificationSessionRegistry:
    def __init__(self) -> None:
        self._lock = Lock()
        self._sessions: dict[str, ClarificationSession] = {}

    def get(self, context_key: str, default_target: str = "auto") -> ClarificationSession:
        with self._lock:
            session = self._sessions.get(context_key)
            if session is None:
                session = ClarificationSession(
                    context_key=context_key,
                    default_target=default_target,
                )
                self._sessions[context_key] = session
            return session

    def update_draft(
        self,
        context_key: str,
        draft: str,
        default_target: str = "auto",
    ) -> ClarificationSession:
        with self._lock:
            session = self._sessions.get(context_key)
            if session is None:
                session = ClarificationSession(
                    context_key=context_key,
                    draft=draft,
                    default_target=default_target,
                )
            else:
                session = session.model_copy(
                    update={"draft": draft, "default_target": default_target}
                )
            self._sessions[context_key] = session
            return session

    def append(
        self,
        context_key: str,
        *,
        draft: str,
        target: str,
        question: str,
        answer: str,
        default_target: str = "auto",
    ) -> ClarificationSession:
        with self._lock:
            session = self._sessions.get(context_key)
            turns = list(session.turns) if session else []
            turns.extend(
                [
                    ClarificationTurn(
                        role=ClarificationRole.USER,
                        content=question,
                        target=target,
                        kind="user_question",
                    ),
                    ClarificationTurn(
                        role=ClarificationRole.ASSISTANT,
                        content=answer,
                        target=target,
                        kind="assistant_answer",
                    ),
                ]
            )
            updated = ClarificationSession(
                context_key=context_key,
                draft=draft,
                default_target=default_target,
                turns=turns,
            )
            self._sessions[context_key] = updated
            return updated

    def append_system_question(
        self,
        context_key: str,
        *,
        draft: str,
        target: str,
        question: str,
        default_target: str = "auto",
    ) -> ClarificationSession:
        with self._lock:
            session = self._sessions.get(context_key)
            turns = list(session.turns) if session else []
            turns.append(
                ClarificationTurn(
                    role=ClarificationRole.ASSISTANT,
                    content=question,
                    target=target,
                    kind="system_question",
                )
            )
            updated = ClarificationSession(
                context_key=context_key,
                draft=draft,
                default_target=default_target,
                turns=turns,
            )
            self._sessions[context_key] = updated
            return updated

    def append_user_answer(
        self,
        context_key: str,
        *,
        draft: str,
        answer: str,
        target: str,
        default_target: str = "auto",
    ) -> ClarificationSession:
        with self._lock:
            session = self._sessions.get(context_key)
            turns = list(session.turns) if session else []
            turns.append(
                ClarificationTurn(
                    role=ClarificationRole.USER,
                    content=answer,
                    target=target,
                    kind="user_answer",
                )
            )
            updated = ClarificationSession(
                context_key=context_key,
                draft=draft,
                default_target=default_target,
                turns=turns,
            )
            self._sessions[context_key] = updated
            return updated

    def append_intake_answers(
        self,
        context_key: str,
        *,
        draft: str,
        pairs: list[tuple[str, str]],
        target: str = "oracle",
        default_target: str = "auto",
    ) -> ClarificationSession:
        with self._lock:
            session = self._sessions.get(context_key)
            turns = list(session.turns) if session else []
            for question, answer in pairs:
                turns.append(
                    ClarificationTurn(
                        role=ClarificationRole.ASSISTANT,
                        content=question,
                        target=target,
                        kind="system_question",
                    )
                )
                turns.append(
                    ClarificationTurn(
                        role=ClarificationRole.USER,
                        content=answer,
                        target=target,
                        kind="user_answer",
                    )
                )
            updated = ClarificationSession(
                context_key=context_key,
                draft=draft,
                default_target=default_target,
                turns=turns,
            )
            self._sessions[context_key] = updated
            return updated

    def list_sessions(self) -> list[ClarificationSession]:
        with self._lock:
            return list(self._sessions.values())

    def reset(self, context_key: str) -> None:
        with self._lock:
            self._sessions.pop(context_key, None)


def serve_app_ui(
    paths: MatrixPaths,
    vault: MemoryVault,
    store: RuntimeStore,
    request_runner: Callable[..., MatrixRunResult],
    agent_request_runner: Callable[..., MatrixRunResult] | None = None,
    intake_runner: Callable[[str], list[ClarifyingQuestion]] | None = None,
    operator: TheOperator | None = None,
    clarifier: ClarificationService | None = None,
    port: int = 0,
    open_browser: bool = True,
    url_callback: Callable[[str], None] | None = None,
    timeout_seconds: int = DEFAULT_APP_TIMEOUT_SECONDS,
) -> str:
    token = token_urlsafe(24)
    run_lock = Lock()
    mission_registry = MissionRegistry()
    oracle_registry = OracleJobRegistry()
    clarification_registry = ClarificationSessionRegistry()
    approval_registry = ApprovalRegistry()
    active_operator = operator or TheOperator(store)
    active_clarifier = clarifier or ClarificationService(store, default_model_gateway(store))
    active_operator.start()
    server = _AppServer(
        ("127.0.0.1", port),
        _handler_factory(
            paths,
            vault,
            store,
            token,
            request_runner,
            agent_request_runner,
            intake_runner,
            active_operator,
            active_clarifier,
            run_lock,
            mission_registry,
            oracle_registry,
            clarification_registry,
            approval_registry,
        ),
    )
    host, bound_port = server.server_address
    url = f"http://{host}:{bound_port}/dashboard?token={token}"
    if url_callback is not None:
        url_callback(url)
    if open_browser:
        webbrowser.open(url)
    timer = Timer(timeout_seconds, server.shutdown)
    timer.daemon = True
    timer.start()
    try:
        server.serve_forever()
    finally:
        timer.cancel()
        active_operator.stop()
        server.server_close()
    return url


class _AppServer(ThreadingHTTPServer):
    allow_reuse_address = False


def _handler_factory(
    paths: MatrixPaths,
    vault: MemoryVault,
    store: RuntimeStore,
    token: str,
    request_runner: Callable[..., MatrixRunResult],
    agent_request_runner: Callable[..., MatrixRunResult] | None,
    intake_runner: Callable[[str], list[ClarifyingQuestion]] | None,
    operator: TheOperator,
    clarifier: ClarificationService,
    run_lock: Lock,
    mission_registry: MissionRegistry,
    oracle_registry: OracleJobRegistry,
    clarification_registry: ClarificationSessionRegistry,
    approval_registry: ApprovalRegistry,
) -> AgentSpec:
    oauth_flows: dict[str, OAuthPendingSetup] = {}
    oauth_lock = Lock()

    class AppHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/oauth/openrouter/callback":
                self._complete_openrouter_oauth(parsed)
                return
            if not self._token_ok():
                self._send_html(HTTPStatus.FORBIDDEN, _message_page("Forbidden", "Invalid token."))
                return
            if parsed.path == "/oauth/openrouter/start":
                self._start_openrouter_oauth(parsed)
                return
            if parsed.path == "/":
                self._send_html(
                    HTTPStatus.OK,
                    render_app_page(
                        paths,
                        store,
                        token,
                        clarification_session=clarification_registry.get("mission"),
                        pending_actions=_pending_user_actions(
                            clarification_registry,
                            approval_registry,
                            token,
                        ),
                    ),
                )
                return
            if parsed.path == "/settings":
                self._send_html(
                    HTTPStatus.OK,
                    render_setup_form(
                        token,
                        detections=detect_local_providers(timeout_seconds=0.5),
                        dashboard_url=f"/dashboard?token={token}",
                        current_config=store.get_default_provider_config(),
                    ),
                )
                return
            if parsed.path == "/dashboard":
                write_dashboard(paths, store)
                self._send_html(
                    HTTPStatus.OK,
                    render_dashboard_html(
                        paths,
                        store,
                        token,
                        pending_actions=_pending_user_actions(
                            clarification_registry,
                            approval_registry,
                            token,
                        ),
                    ),
                )
                return
            if parsed.path == "/oracle":
                self._send_html(
                    HTTPStatus.OK,
                    _oracle_page(
                        token,
                        clarification_registry.get("oracle", "oracle"),
                    ),
                )
                return
            if parsed.path == "/oracle/status":
                query = parse_qs(parsed.query)
                job_id = query.get("job_id", [""])[-1].strip()
                payload = _oracle_job_payload(oracle_registry.get(job_id))
                status = HTTPStatus.OK if payload["found"] else HTTPStatus.NOT_FOUND
                self._send_json(status, payload)
                return
            if parsed.path == "/mission":
                query = parse_qs(parsed.query)
                job_id = query.get("job_id", [""])[-1].strip()
                run_id = query.get("run_id", [""])[-1].strip()
                self._send_html(
                    HTTPStatus.OK,
                    _mission_page(
                        store,
                        token,
                        mission_registry.get(job_id),
                        run_id=run_id,
                        approval_registry=approval_registry,
                    ),
                )
                return
            if parsed.path == "/mission/status":
                query = parse_qs(parsed.query)
                job_id = query.get("job_id", [""])[-1].strip()
                run_id = query.get("run_id", [""])[-1].strip()
                payload = _mission_payload(
                    store,
                    mission_registry.get(job_id),
                    run_id=run_id,
                    approval_registry=approval_registry,
                )
                status = HTTPStatus.OK if payload["found"] else HTTPStatus.NOT_FOUND
                self._send_json(status, payload)
                return
            if parsed.path == "/agent":
                agent_id = parse_qs(parsed.query).get("agent_id", [""])[-1].strip()
                if not agent_id:
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        _message_page("Agent missing", "Choose an agent from the dashboard."),
                    )
                    return
                status = HTTPStatus.OK if store.get_agent(agent_id) is not None else HTTPStatus.NOT_FOUND
                self._send_html(
                    status,
                    _agent_page(
                        paths,
                        store,
                        token,
                        agent_id,
                        clarification_session=clarification_registry.get(
                            _agent_context_key(agent_id),
                            f"agent:{agent_id}",
                        ),
                    ),
                )
                return
            if parsed.path == "/diagnostics":
                self._send_html(HTTPStatus.OK, _diagnostics_page(paths, store, token))
                return
            if parsed.path == "/memory":
                self._send_html(HTTPStatus.OK, _memory_page(paths, store, token))
                return
            if parsed.path == "/operator":
                goal_id = parse_qs(parsed.query).get("goal_id", [""])[-1].strip()
                self._send_html(HTTPStatus.OK, _operator_page(store, token, goal_id=goal_id))
                return
            self._send_html(HTTPStatus.NOT_FOUND, _message_page("Not Found", "Unknown route."))

        def do_POST(self) -> None:
            if not self._token_ok():
                self._discard_request_body()
                self._send_html(HTTPStatus.FORBIDDEN, _message_page("Forbidden", "Invalid token."))
                return
            parsed = urlparse(self.path)
            if parsed.path == "/shutdown":
                self._send_html(
                    HTTPStatus.OK,
                    _message_page("App stopped", "The local Matrix app has been stopped."),
                )
                Thread(target=self.server.shutdown, daemon=True).start()
                return
            if parsed.path == "/approval/respond":
                form = self._read_form(paths, store, token)
                if form is None:
                    return
                approval_id = form.get("approval_id", "").strip()
                decision = form.get("decision", "").strip()
                return_to = form.get("return_to", "").strip()
                if decision not in {"approve", "deny"}:
                    if return_to == "dashboard":
                        self._send_html(
                            HTTPStatus.BAD_REQUEST,
                            render_dashboard_html(
                                paths,
                                store,
                                token,
                                pending_actions=_pending_user_actions(
                                    clarification_registry,
                                    approval_registry,
                                    token,
                                ),
                            ),
                        )
                        return
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "Choose approve or deny."},
                    )
                    return
                approval = approval_registry.respond(approval_id, decision == "approve")
                if approval is None:
                    if return_to == "dashboard":
                        self._send_html(
                            HTTPStatus.NOT_FOUND,
                            render_dashboard_html(
                                paths,
                                store,
                                token,
                                pending_actions=_pending_user_actions(
                                    clarification_registry,
                                    approval_registry,
                                    token,
                                ),
                            ),
                        )
                        return
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error": "Approval request not found."},
                    )
                    return
                if return_to == "dashboard":
                    self._send_html(
                        HTTPStatus.OK,
                        render_dashboard_html(
                            paths,
                            store,
                            token,
                            pending_actions=_pending_user_actions(
                                clarification_registry,
                                approval_registry,
                                token,
                            ),
                        ),
                    )
                    return
                if return_to == "app":
                    self._send_html(
                        HTTPStatus.OK,
                        render_app_page(
                            paths,
                            store,
                            token,
                            AppUiResponse(message=f"Approval {approval.status}."),
                            clarification_session=clarification_registry.get("mission"),
                            pending_actions=_pending_user_actions(
                                clarification_registry,
                                approval_registry,
                                token,
                            ),
                        ),
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "approval_id": approval.approval_id,
                        "status": approval.status,
                    },
                )
                return
            if parsed.path == "/save":
                form = self._read_form(paths, store, token)
                if form is None:
                    return
                result = apply_setup_form(form, paths, vault, store, Keymaker())
                if not result.ok:
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        render_setup_form(
                            token,
                            error=result.message,
                            detections=detect_local_providers(timeout_seconds=0.5),
                            dashboard_url=f"/dashboard?token={token}",
                            current_config=store.get_default_provider_config(),
                        ),
                    )
                    return
                self._send_html(
                    HTTPStatus.OK,
                    render_app_page(paths, store, token, AppUiResponse(message=result.message)),
                )
                return
            if parsed.path == "/clarify/intent":
                form = self._read_form(paths, store, token)
                if form is None:
                    return
                context_key = _clarification_context_from_form(form)
                default_target = _default_target_for_context(context_key)
                draft = form.get("draft", "").strip()
                target = form.get("target", default_target).strip() or default_target
                agent_id = form.get("agent_id", "").strip()
                if not draft:
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        _render_context_page(
                            paths,
                            store,
                            token,
                            context_key,
                            agent_id,
                            clarification_registry.get(context_key, default_target),
                            AppUiResponse(error="Describe the mission before checking intent."),
                        ),
                    )
                    return
                try:
                    session, message = _ask_next_intent_question(
                        clarifier,
                        clarification_registry,
                        context_key=context_key,
                        draft=draft,
                        target=target,
                        default_target=default_target,
                    )
                except (ClarificationError, Exception) as exc:
                    response = AppUiResponse(error=f"Intent check failed: {exc}")
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        _render_context_page(
                            paths,
                            store,
                            token,
                            context_key,
                            agent_id,
                            clarification_registry.get(context_key, default_target),
                            response,
                        ),
                    )
                    return
                self._send_html(
                    HTTPStatus.OK,
                    _render_context_page(
                        paths,
                        store,
                        token,
                        context_key,
                        agent_id,
                        session,
                        AppUiResponse(message=message),
                    ),
                )
                return
            if parsed.path == "/clarify/answer":
                form = self._read_form(paths, store, token)
                if form is None:
                    return
                context_key = _clarification_context_from_form(form)
                default_target = _default_target_for_context(context_key)
                draft = form.get("draft", "").strip()
                answer = form.get("answer", "").strip()
                agent_id = form.get("agent_id", "").strip()
                session = clarification_registry.update_draft(context_key, draft, default_target)
                pending = _pending_clarification_question(session)
                if pending is None:
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        _render_context_page(
                            paths,
                            store,
                            token,
                            context_key,
                            agent_id,
                            session,
                            AppUiResponse(error="Ask The Matrix for an intent question first."),
                        ),
                    )
                    return
                if not answer:
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        _render_context_page(
                            paths,
                            store,
                            token,
                            context_key,
                            agent_id,
                            session,
                            AppUiResponse(error="Answer the Matrix question before continuing."),
                        ),
                    )
                    return
                answered = clarification_registry.append_user_answer(
                    context_key,
                    draft=draft,
                    answer=answer,
                    target=pending.target,
                    default_target=pending.target,
                )
                try:
                    session, message = _ask_next_intent_question(
                        clarifier,
                        clarification_registry,
                        context_key=context_key,
                        draft=draft,
                        target=pending.target,
                        default_target=pending.target,
                    )
                except (ClarificationError, Exception) as exc:
                    session = answered
                    message = f"Answer saved, but the follow-up intent check failed: {exc}"
                self._send_html(
                    HTTPStatus.OK,
                    _render_context_page(
                        paths,
                        store,
                        token,
                        context_key,
                        agent_id,
                        session,
                        AppUiResponse(message=message),
                    ),
                )
                return
            if parsed.path == "/clarify":
                form = self._read_form(paths, store, token)
                if form is None:
                    return
                context_key = _clarification_context_from_form(form)
                default_target = _default_target_for_context(context_key)
                draft = form.get("draft", "").strip()
                target = form.get("target", default_target).strip() or default_target
                question = form.get("question", "").strip()
                agent_id = form.get("agent_id", "").strip()
                current_session = clarification_registry.update_draft(
                    context_key,
                    draft,
                    default_target,
                )
                try:
                    clarification = clarifier.answer(
                        draft=draft,
                        question=question,
                        target=target,
                        transcript=current_session.turns,
                    )
                except (ClarificationError, Exception) as exc:
                    response = AppUiResponse(error=f"Clarification failed: {exc}")
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        _render_context_page(
                            paths,
                            store,
                            token,
                            context_key,
                            agent_id,
                            clarification_registry.get(context_key, default_target),
                            response,
                        ),
                    )
                    return
                session = clarification_registry.append(
                    context_key,
                    draft=draft,
                    target=clarification.target,
                    question=question,
                    answer=clarification.answer,
                    default_target=clarification.target,
                )
                self._send_html(
                    HTTPStatus.OK,
                    _render_context_page(
                        paths,
                        store,
                        token,
                        context_key,
                        agent_id,
                        session,
                        AppUiResponse(message="Clarification added to this mission draft."),
                    ),
                )
                return
            if parsed.path == "/clarify/reset":
                form = self._read_form(paths, store, token)
                if form is None:
                    return
                context_key = _clarification_context_from_form(form)
                agent_id = form.get("agent_id", "").strip()
                clarification_registry.reset(context_key)
                self._send_html(
                    HTTPStatus.OK,
                    _render_context_page(
                        paths,
                        store,
                        token,
                        context_key,
                        agent_id,
                        clarification_registry.get(
                            context_key,
                            _default_target_for_context(context_key),
                        ),
                        AppUiResponse(message="Clarification transcript cleared."),
                    ),
                )
                return
            if parsed.path == "/oracle/ask":
                form = self._read_form(paths, store, token)
                if form is None:
                    return
                question = form.get("question", "").strip()
                session = clarification_registry.get("oracle", "oracle")
                if not question:
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        _oracle_page(
                            token,
                            session,
                            AppUiResponse(error="Ask the Oracle a question first."),
                        ),
                    )
                    return
                job = oracle_registry.create(question)
                Thread(
                    target=_run_oracle_question,
                    args=(job, clarifier, clarification_registry, session.turns),
                    daemon=True,
                ).start()
                self._send_html(
                    HTTPStatus.OK,
                    _oracle_page(
                        token,
                        session,
                        AppUiResponse(message="Oracle transmission started."),
                        oracle_job=job,
                    ),
                )
                return
            if parsed.path == "/ask":
                form = self._read_form(paths, store, token)
                if form is None:
                    return
                user_request = form.get("request", "").strip()
                if not user_request:
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        render_app_page(
                            paths,
                            store,
                            token,
                            AppUiResponse(error="Enter a request before transmitting."),
                        ),
                    )
                    return
                session = clarification_registry.update_draft("mission", user_request)
                if _pending_clarification_question(session) is not None:
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        render_app_page(
                            paths,
                            store,
                            token,
                            AppUiResponse(
                                error="Answer the Matrix intent question before running."
                            ),
                            clarification_session=session,
                        ),
                    )
                    return
                if intake_runner is not None and not session.has_turns:
                    questions = self._intake_questions(intake_runner, user_request)
                    if questions:
                        self._send_html(
                            HTTPStatus.OK,
                            render_app_page(
                                paths,
                                store,
                                token,
                                AppUiResponse(
                                    message=(
                                        "The Matrix needs a few details before spawning "
                                        "agents for this mission."
                                    ),
                                ),
                                clarification_session=session,
                                intake_questions=questions,
                            ),
                        )
                        return
                elif intake_runner is None:
                    session, ready = _ensure_ready_or_ask(
                        clarifier,
                        clarification_registry,
                        context_key="mission",
                        draft=user_request,
                        target=session.default_target or "auto",
                        default_target=session.default_target or "auto",
                    )
                    if not ready:
                        self._send_html(
                            HTTPStatus.OK,
                            render_app_page(
                                paths,
                                store,
                                token,
                                AppUiResponse(
                                    message=(
                                        "The Matrix needs one more detail before starting "
                                        "this mission."
                                    ),
                                ),
                                clarification_session=session,
                            ),
                        )
                        return
                launch_request = _clarified_launch_request(clarifier, user_request, session)
                self._launch_mission(launch_request)
                return
            if parsed.path == "/intake/submit":
                form = self._read_form(paths, store, token)
                if form is None:
                    return
                user_request = (
                    form.get("request", "").strip()
                    or clarification_registry.get("mission").draft
                )
                pairs = _intake_pairs_from_form(form)
                session = clarification_registry.append_intake_answers(
                    "mission",
                    draft=user_request,
                    pairs=pairs,
                )
                launch_request = _clarified_launch_request(clarifier, user_request, session)
                self._launch_mission(launch_request)
                return
            if parsed.path == "/operator/action":
                form = self._read_form(paths, store, token)
                if form is None:
                    return
                goal_id = form.get("goal_id", "").strip()
                action = form.get("action", "").strip()
                return_to = form.get("return_to", "").strip()
                try:
                    if action == "activate":
                        operator.activate_goal(goal_id)
                    elif action == "pause":
                        operator.pause_goal(goal_id)
                    elif action == "resume":
                        operator.resume_goal(goal_id)
                    elif action == "cancel":
                        operator.cancel_goal(goal_id)
                    elif action == "run_now":
                        operator.run_goal_now(goal_id)
                    else:
                        raise ValueError("Choose a valid Operator action.")
                except ValueError as exc:
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        _operator_page(store, token, AppUiResponse(error=str(exc))),
                    )
                    return
                if return_to == "dashboard":
                    self._send_html(
                        HTTPStatus.OK,
                        render_dashboard_html(
                            paths,
                            store,
                            token,
                            pending_actions=_pending_user_actions(
                                clarification_registry,
                                approval_registry,
                                token,
                            ),
                        ),
                    )
                    return
                self._send_html(
                    HTTPStatus.OK,
                    _operator_page(store, token, response=AppUiResponse(message="Operator goal updated.")),
                )
                return
            if parsed.path == "/operator/update":
                form = self._read_form(paths, store, token)
                if form is None:
                    return
                goal_id = form.get("goal_id", "").strip()
                try:
                    interval_minutes = int(form.get("interval_minutes", "0"))
                    updated = operator.update_recurring_notification_goal(
                        goal_id,
                        title=form.get("title", ""),
                        message=form.get("message", ""),
                        interval_minutes=interval_minutes,
                    )
                except ValueError as exc:
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        _operator_page(
                            store,
                            token,
                            goal_id=goal_id,
                            response=AppUiResponse(error=str(exc)),
                        ),
                    )
                    return
                self._send_html(
                    HTTPStatus.OK,
                    _operator_page(
                        store,
                        token,
                        goal_id=updated.goal_id,
                        response=AppUiResponse(message="Operator goal updated."),
                    ),
                )
                return
            if parsed.path == "/agent/update":
                form = self._read_form(paths, store, token)
                if form is None:
                    return
                agent_id = form.get("agent_id", "").strip()
                try:
                    spec = _update_agent_from_form(paths, vault, store, form)
                except ValueError as exc:
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        _agent_page(
                            paths,
                            store,
                            token,
                            agent_id,
                            AppUiResponse(error=str(exc)),
                            clarification_session=clarification_registry.get(
                                _agent_context_key(agent_id),
                                f"agent:{agent_id}",
                            ),
                        ),
                    )
                    return
                self._send_html(
                    HTTPStatus.OK,
                    _agent_page(
                        paths,
                        store,
                        token,
                        spec.agent_id,
                        AppUiResponse(message="Agent instructions updated."),
                        clarification_session=clarification_registry.get(
                            _agent_context_key(spec.agent_id),
                            f"agent:{spec.agent_id}",
                        ),
                    ),
                )
                return
            if parsed.path == "/agent/toggle":
                form = self._read_form(paths, store, token)
                if form is None:
                    return
                agent_id = form.get("agent_id", "").strip()
                try:
                    spec = _toggle_agent(paths, vault, store, agent_id)
                except ValueError as exc:
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        _agent_page(
                            paths,
                            store,
                            token,
                            agent_id,
                            AppUiResponse(error=str(exc)),
                            clarification_session=clarification_registry.get(
                                _agent_context_key(agent_id),
                                f"agent:{agent_id}",
                            ),
                        ),
                    )
                    return
                message = "Agent resumed." if spec.enabled else "Agent paused."
                self._send_html(
                    HTTPStatus.OK,
                    _agent_page(
                        paths,
                        store,
                        token,
                        spec.agent_id,
                        AppUiResponse(message=message),
                        clarification_session=clarification_registry.get(
                            _agent_context_key(spec.agent_id),
                            f"agent:{spec.agent_id}",
                        ),
                    ),
                )
                return
            if parsed.path == "/agent/run":
                form = self._read_form(paths, store, token)
                if form is None:
                    return
                agent_id = form.get("agent_id", "").strip()
                user_request = form.get("request", "").strip()
                if not agent_id:
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        _message_page("Agent missing", "Choose an agent from the dashboard."),
                    )
                    return
                if not user_request:
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        _agent_page(
                            paths,
                            store,
                            token,
                            agent_id,
                            AppUiResponse(error="Enter a mission for this agent."),
                            clarification_session=clarification_registry.get(
                                _agent_context_key(agent_id),
                                f"agent:{agent_id}",
                            ),
                        ),
                    )
                    return
                if agent_request_runner is None:
                    self._send_html(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        _agent_page(
                            paths,
                            store,
                            token,
                            agent_id,
                            AppUiResponse(
                                error="Manual agent runs are not available in this session."
                            ),
                            clarification_session=clarification_registry.get(
                                _agent_context_key(agent_id),
                                f"agent:{agent_id}",
                            ),
                        ),
                    )
                    return
                spec = store.get_agent(agent_id)
                if spec is not None and not spec.enabled:
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        _agent_page(
                            paths,
                            store,
                            token,
                            agent_id,
                            AppUiResponse(error="Resume this agent before running it."),
                            clarification_session=clarification_registry.get(
                                _agent_context_key(agent_id),
                                f"agent:{agent_id}",
                            ),
                        ),
                    )
                    return
                context_key = _agent_context_key(agent_id)
                session = clarification_registry.update_draft(
                    context_key,
                    user_request,
                    f"agent:{agent_id}",
                )
                if _pending_clarification_question(session) is not None:
                    self._send_html(
                        HTTPStatus.BAD_REQUEST,
                        _agent_page(
                            paths,
                            store,
                            token,
                            agent_id,
                            AppUiResponse(
                                error="Answer the Matrix intent question before running this agent."
                            ),
                            clarification_session=session,
                        ),
                    )
                    return
                session, ready = _ensure_ready_or_ask(
                    clarifier,
                    clarification_registry,
                    context_key=context_key,
                    draft=user_request,
                    target=session.default_target or f"agent:{agent_id}",
                    default_target=session.default_target or f"agent:{agent_id}",
                )
                if not ready:
                    self._send_html(
                        HTTPStatus.OK,
                        _agent_page(
                            paths,
                            store,
                            token,
                            agent_id,
                            AppUiResponse(
                                message=(
                                    "The Matrix needs one more detail before running "
                                    "this agent."
                                ),
                            ),
                            clarification_session=session,
                        ),
                    )
                    return
                if not run_lock.acquire(blocking=False):
                    self._send_html(
                        HTTPStatus.CONFLICT,
                        _agent_page(
                            paths,
                            store,
                            token,
                            agent_id,
                            AppUiResponse(
                                busy=True,
                                message=(
                                    "A mission is already running. This click did not start "
                                    "a duplicate agent run."
                                ),
                            ),
                            clarification_session=clarification_registry.get(
                                _agent_context_key(agent_id),
                                f"agent:{agent_id}",
                            ),
                        ),
                    )
                    return
                launch_request = _clarified_launch_request(clarifier, user_request, session)
                goal = operator.create_one_shot_goal(
                    launch_request,
                    title=f"Run {agent_id}",
                    payload={"agent_id": agent_id},
                )
                job = mission_registry.create("agent", launch_request, agent_id=agent_id)
                Thread(
                    target=_run_background_agent_mission,
                    args=(
                        job,
                        agent_request_runner,
                        agent_id,
                        launch_request,
                        run_lock,
                        approval_registry,
                        operator,
                        goal.goal_id,
                    ),
                    daemon=True,
                ).start()
                clarification_registry.reset(context_key)
                self._send_html(
                    HTTPStatus.ACCEPTED,
                    _mission_page(
                        store,
                        token,
                        job,
                        approval_registry=approval_registry,
                    ),
                )
                return
            self._send_html(HTTPStatus.NOT_FOUND, _message_page("Not Found", "Unknown route."))

        def _start_openrouter_oauth(self, parsed) -> None:
            form = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            host, port = self.server.server_address[:2]

            def callback_url_for_flow(flow_id: str, state: str) -> str:
                return (
                    f"http://{host}:{port}/oauth/openrouter/callback?"
                    + urlencode({"flow": flow_id, "state": state})
                )

            try:
                start, pending = build_openrouter_oauth_setup(form, callback_url_for_flow)
            except OAuthProviderError as exc:
                self._send_html(
                    HTTPStatus.BAD_REQUEST,
                    _message_page("OAuth unavailable", str(exc)),
                )
                return
            with oauth_lock:
                oauth_flows[start.flow_id] = pending
            self._send_redirect(start.authorization_url)

        def _complete_openrouter_oauth(self, parsed) -> None:
            query = parse_qs(parsed.query)
            flow_id = query.get("flow", [""])[-1]
            state = query.get("state", [""])[-1]
            code = query.get("code", [""])[-1]
            if not flow_id or not state or not code:
                self._send_html(
                    HTTPStatus.BAD_REQUEST,
                    _message_page("OAuth incomplete", "The provider did not return a usable code."),
                )
                return
            with oauth_lock:
                pending = oauth_flows.get(flow_id)
            if pending is None:
                self._send_html(
                    HTTPStatus.BAD_REQUEST,
                    _message_page("OAuth expired", "Start provider sign-in again from settings."),
                )
                return
            if not hmac.compare_digest(state, pending.callback_state):
                self._send_html(
                    HTTPStatus.FORBIDDEN,
                    _message_page("OAuth rejected", "The provider sign-in state did not match."),
                )
                return
            self._session_verified = True
            with oauth_lock:
                oauth_flows.pop(flow_id, None)
            try:
                api_key = exchange_openrouter_code(code, pending.code_verifier)
            except (OAuthProviderError, OSError, ValueError) as exc:
                self._send_html(
                    HTTPStatus.BAD_GATEWAY,
                    _message_page("OAuth failed", f"OpenRouter sign-in failed: {exc}"),
                )
                return
            form = setup_form_from_oauth(pending, api_key)
            result = apply_setup_form(form, paths, vault, store, Keymaker())
            if not result.ok:
                self._send_html(
                    HTTPStatus.BAD_REQUEST,
                    render_setup_form(
                        token,
                        error=result.message,
                        detections=detect_local_providers(timeout_seconds=0.5),
                        dashboard_url=f"/dashboard?token={token}",
                        current_config=store.get_default_provider_config(),
                    ),
                )
                return
            write_dashboard(paths, store)
            self._send_html(
                HTTPStatus.OK,
                render_app_page(paths, store, token, AppUiResponse(message=result.message)),
            )

        def _read_form(
            self,
            paths: MatrixPaths,
            store: RuntimeStore,
            token: str,
        ) -> dict[str, str] | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_html(
                    HTTPStatus.BAD_REQUEST,
                    render_app_page(
                        paths,
                        store,
                        token,
                        AppUiResponse(error="Content-Length must be a number."),
                    ),
                )
                return None
            if length > MAX_APP_BODY_BYTES:
                self._send_html(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    render_app_page(
                        paths,
                        store,
                        token,
                        AppUiResponse(error="Request payload is too large."),
                    ),
                )
                return None

            raw = self.rfile.read(length).decode("utf-8")
            return {key: values[-1] for key, values in parse_qs(raw).items()}

        def _intake_questions(
            self,
            runner: Callable[[str], list[ClarifyingQuestion]],
            draft: str,
        ) -> list[ClarifyingQuestion]:
            try:
                return list(runner(draft) or [])
            except Exception:
                return []

        def _launch_mission(self, launch_request: str) -> None:
            operator_goal = operator.create_from_request(launch_request)
            if operator_goal is not None:
                clarification_registry.reset("mission")
                self._send_html(
                    HTTPStatus.OK,
                    render_app_page(
                        paths,
                        store,
                        token,
                        AppUiResponse(
                            message=(
                                "The Operator drafted a recurring goal. "
                                "Open The Operator to review and activate it if it should keep running."
                            ),
                        ),
                    ),
                )
                return
            if not run_lock.acquire(blocking=False):
                self._send_html(
                    HTTPStatus.CONFLICT,
                    render_app_page(
                        paths,
                        store,
                        token,
                        AppUiResponse(
                            busy=True,
                            message=(
                                "A mission is already running. This click did not start "
                                "a duplicate mission."
                            ),
                        ),
                    ),
                )
                return
            goal = operator.create_one_shot_goal(launch_request)
            job = mission_registry.create("mission", launch_request)
            Thread(
                target=_run_background_mission,
                args=(
                    job,
                    request_runner,
                    launch_request,
                    run_lock,
                    approval_registry,
                    operator,
                    goal.goal_id,
                ),
                daemon=True,
            ).start()
            clarification_registry.reset("mission")
            self._send_html(
                HTTPStatus.ACCEPTED,
                _mission_page(
                    store,
                    token,
                    job,
                    approval_registry=approval_registry,
                ),
            )

        def _discard_request_body(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return
            if length > 0:
                self.rfile.read(length)

        def log_message(self, format: str, *args: object) -> None:
            return

        def _token_ok(self) -> bool:
            query = parse_qs(urlparse(self.path).query)
            supplied = query.get("token", [""])[-1]
            if hmac.compare_digest(supplied, token) or self._session_cookie_ok():
                self._session_verified = True
                return True
            return False

        def _session_cookie_ok(self) -> bool:
            cookie_header = self.headers.get("Cookie", "")
            if not cookie_header:
                return False
            cookie = SimpleCookie()
            try:
                cookie.load(cookie_header)
            except Exception:
                return False
            morsel = cookie.get(APP_SESSION_COOKIE_NAME)
            return bool(morsel) and hmac.compare_digest(morsel.value, token)

        def _send_session_cookie_if_verified(self) -> None:
            if getattr(self, "_session_verified", False):
                self.send_header(
                    "Set-Cookie",
                    f"{APP_SESSION_COOKIE_NAME}={token}; {SESSION_COOKIE_ATTRIBUTES}",
                )

        def _send_redirect(self, location: str) -> None:
            self.send_response(HTTPStatus.FOUND.value)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self._send_session_cookie_if_verified()
            self.end_headers()

        def _send_html(self, status: HTTPStatus, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self._send_session_cookie_if_verified()
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self._send_session_cookie_if_verified()
            self.end_headers()
            self.wfile.write(body)

    return AppHandler


def _run_background_mission(
    job: MissionJob,
    request_runner: Callable[..., MatrixRunResult],
    request: str,
    run_lock: Lock,
    approval_registry: ApprovalRegistry,
    operator: TheOperator | None = None,
    goal_id: str | None = None,
) -> None:
    job.start()
    try:
        result = _call_runner(
            request_runner,
            request,
            job.record,
            approval_callback=lambda target, reason, purpose: approval_registry.request(
                job,
                target,
                reason,
                purpose,
            ),
        )
    except Exception as exc:
        job.fail(f"Mission failed: {exc}")
        if operator is not None and goal_id:
            operator.fail_goal(goal_id, f"Mission failed: {exc}")
    else:
        job.complete(result)
        if operator is not None and goal_id:
            operator.complete_goal(
                goal_id,
                "Mission completed.",
                {"run_id": result.run_id, "kind": job.kind},
            )
    finally:
        run_lock.release()


def _run_background_agent_mission(
    job: MissionJob,
    agent_request_runner: Callable[..., MatrixRunResult],
    agent_id: str,
    request: str,
    run_lock: Lock,
    approval_registry: ApprovalRegistry,
    operator: TheOperator | None = None,
    goal_id: str | None = None,
) -> None:
    job.start()
    try:
        result = _call_agent_runner(
            agent_request_runner,
            agent_id,
            request,
            job.record,
            approval_callback=lambda target, reason, purpose: approval_registry.request(
                job,
                target,
                reason,
                purpose,
            ),
        )
    except Exception as exc:
        job.fail(f"Agent run failed: {exc}")
        if operator is not None and goal_id:
            operator.fail_goal(goal_id, f"Agent run failed: {exc}", {"agent_id": agent_id})
    else:
        job.complete(result)
        if operator is not None and goal_id:
            operator.complete_goal(
                goal_id,
                "Agent run completed.",
                {"run_id": result.run_id, "agent_id": agent_id, "kind": job.kind},
            )
    finally:
        run_lock.release()


def _run_oracle_question(
    job: OracleJob,
    clarifier: ClarificationService,
    clarification_registry: ClarificationSessionRegistry,
    transcript: list[ClarificationTurn],
) -> None:
    try:
        clarification = clarifier.answer(
            draft="",
            question=job.question,
            target="oracle",
            transcript=transcript,
        )
    except (ClarificationError, Exception) as exc:
        job.fail(str(exc))
        return
    clarification_registry.append(
        "oracle",
        draft="",
        target=clarification.target,
        question=job.question,
        answer=clarification.answer,
        default_target="oracle",
    )
    job.complete(clarification.answer)


def _call_runner(
    runner: Callable[..., MatrixRunResult],
    request: str,
    progress_callback: Callable[[str, str, dict[str, object]], None],
    approval_callback: Callable[[str, str, str], bool] | None = None,
) -> MatrixRunResult:
    kwargs = {}
    if _accepts_callback(runner, "progress_callback"):
        kwargs["progress_callback"] = progress_callback
    if approval_callback is not None and _accepts_callback(runner, "approval_callback"):
        kwargs["approval_callback"] = approval_callback
    if kwargs:
        return runner(request, **kwargs)
    return runner(request)


def _call_agent_runner(
    runner: Callable[..., MatrixRunResult],
    agent_id: str,
    request: str,
    progress_callback: Callable[[str, str, dict[str, object]], None],
    approval_callback: Callable[[str, str, str], bool] | None = None,
) -> MatrixRunResult:
    kwargs = {}
    if _accepts_callback(runner, "progress_callback"):
        kwargs["progress_callback"] = progress_callback
    if approval_callback is not None and _accepts_callback(runner, "approval_callback"):
        kwargs["approval_callback"] = approval_callback
    if kwargs:
        return runner(agent_id, request, **kwargs)
    return runner(agent_id, request)


def _accepts_callback(runner: Callable[..., MatrixRunResult], name: str) -> bool:
    try:
        parameters = signature(runner).parameters.values()
    except (TypeError, ValueError):
        return False
    for parameter in parameters:
        if parameter.kind == Parameter.VAR_KEYWORD:
            return True
        if parameter.name == name:
            return True
    return False


def _mission_page(
    store: RuntimeStore,
    token: str,
    job: MissionJob | None,
    run_id: str = "",
    approval_registry: ApprovalRegistry | None = None,
) -> str:
    payload = _mission_payload(
        store,
        job,
        run_id=run_id,
        approval_registry=approval_registry,
    )
    if not payload["found"]:
        return _utility_page(
            "Mission Not Found",
            token,
            "No mission record exists for that id.",
            "",
        )

    primary_action_label = "New Mission"
    primary_action_url = _token_url("/", token)
    agent_id = payload.get("agent_id")
    if isinstance(agent_id, str) and agent_id and payload.get("agent_available"):
        primary_action_label = "Ask This Agent"
        primary_action_url = _token_url("/agent", token, agent_id=agent_id)

    query = {"token": token}
    if job is not None:
        query["job_id"] = job.job_id
    elif payload.get("run_id"):
        query["run_id"] = str(payload["run_id"])
    status_url = "/mission/status?" + urlencode(query)
    content = """
      <div class="mission-cockpit">
        <div class="mission-summary">
          <p class="kicker">Current status</p>
          <h2 id="mission-state">Loading</h2>
          <p id="mission-message" class="muted"></p>
          <p id="mission-request" class="request-text"></p>
        </div>
        <div id="mission-approvals"></div>
        <div id="mission-contract"></div>
        <div id="mission-result"></div>
        <div class="mission-grid">
          <div>
            <h2>Mission Timeline</h2>
            <div id="mission-events" class="timeline"></div>
          </div>
          <div>
            <h2>Task Ledger</h2>
            <div id="mission-tasks" class="timeline"></div>
          </div>
        </div>
      </div>
"""
    return _utility_page(
        "Mission Status",
        token,
        "Track what the agents are doing and open the result when it is ready.",
        content,
        extra_script=_mission_status_script(
            status_url,
            payload,
            _token_url("/agent", token),
            f"/approval/respond?token={token}",
        ),
        primary_action_label=primary_action_label,
        primary_action_url=primary_action_url,
    )


def _mission_payload(
    store: RuntimeStore,
    job: MissionJob | None,
    run_id: str = "",
    approval_registry: ApprovalRegistry | None = None,
) -> dict[str, object]:
    if job is not None:
        with job.lock:
            result = job.result
            resolved_run_id = result.run_id if result is not None else ""
            resolved_agent_id = job.agent_id or _result_agent_id(result)
            events = list(job.events)
            payload = {
                "found": True,
                "job_id": job.job_id,
                "kind": job.kind,
                "status": job.status,
                "stage": job.stage,
                "message": job.message,
                "request": job.request,
                "agent_id": resolved_agent_id,
                "agent_available": _agent_available(store, resolved_agent_id),
                "created_at": job.created_at,
                "started_at": job.started_at,
                "completed_at": job.completed_at,
                "run_id": resolved_run_id,
                "events": [_event_payload(event) for event in events],
                "mission_context": _mission_context_from_result(result)
                if result is not None
                else _mission_context_from_events(events),
                "tasks": _task_payloads(store, resolved_run_id),
                "result": _result_payload(result),
                "error": job.error,
                "approvals": approval_registry.pending_payloads(job.job_id)
                if approval_registry is not None
                else [],
            }
            if not payload["tasks"]:
                payload["tasks"] = _task_payloads_from_events(events)
            return payload

    if run_id:
        result = store.get_run(run_id)
        if result is None:
            return {"found": False}
        agent_id = _result_agent_id(result)
        return {
            "found": True,
            "job_id": "",
            "kind": "recorded",
            "status": _run_status(result),
            "stage": "recorded",
            "message": "This mission is recorded in local memory.",
            "request": result.request,
            "agent_id": agent_id,
            "agent_available": _agent_available(store, agent_id),
            "created_at": result.created_at.isoformat(),
            "started_at": None,
            "completed_at": result.created_at.isoformat(),
            "run_id": result.run_id,
            "events": _events_from_result(result),
            "mission_context": _mission_context_from_result(result),
            "tasks": _task_payloads(store, result.run_id),
            "result": _result_payload(result),
            "error": result.metadata.get("agent_execution_error"),
            "approvals": [],
        }
    return {"found": False}


def _oracle_job_payload(job: OracleJob | None) -> dict[str, object]:
    if job is None:
        return {"found": False}
    with job.lock:
        return {
            "found": True,
            "job_id": job.job_id,
            "question": job.question,
            "status": job.status,
            "message": job.message,
            "answer": job.answer,
            "error": job.error,
            "created_at": job.created_at,
            "completed_at": job.completed_at,
        }


def _result_agent_id(result: MatrixRunResult | None) -> str | None:
    if result is None or result.agent_spec is None:
        return None
    return result.agent_spec.agent_id


def _agent_available(store: RuntimeStore, agent_id: str | None) -> bool:
    return bool(agent_id and store.get_agent(agent_id) is not None)


def _mission_context_from_result(result: MatrixRunResult | None) -> dict[str, object]:
    if result is None:
        return {}
    return {
        "intent": result.oracle_brief.intent,
        "need": result.oracle_brief.human_need,
        "success_criteria": result.oracle_brief.success_criteria,
        "constraints": result.oracle_brief.constraints,
        "strategy": result.metadata.get("mission_strategy", "sequential"),
        "execution_status": result.metadata.get("agent_execution_status"),
        "task_count": result.metadata.get("mission_task_count"),
        "completed_count": result.metadata.get("mission_completed_count"),
    }


def _mission_context_from_events(events: list[MissionStatusEvent]) -> dict[str, object]:
    for event in reversed(events):
        details = event.details
        if "mission_need" not in details and "success_criteria" not in details:
            continue
        return {
            "intent": _string_value(details.get("intent")),
            "need": _string_value(details.get("mission_need")),
            "success_criteria": _string_list(details.get("success_criteria")),
            "constraints": _string_list(details.get("constraints")),
            "strategy": _string_value(details.get("strategy")),
            "task_count": details.get("task_count"),
            "completed_count": 0,
        }
    return {}


def _event_payload(event: MissionStatusEvent) -> dict[str, object]:
    return {
        "stage": event.stage,
        "message": event.message,
        "details": event.details,
        "created_at": event.created_at,
    }


def _events_from_result(result: MatrixRunResult) -> list[dict[str, object]]:
    events = [
        {
            "stage": "recorded",
            "message": "Mission was recorded in the local vault.",
            "details": {"run_id": result.run_id},
            "created_at": result.created_at.isoformat(),
        }
    ]
    decisions = result.metadata.get("architect_decisions") or []
    for decision in decisions:
        events.append(
            {
                "stage": "architect",
                "message": str(decision.get("decision") or "Architect recorded a decision."),
                "details": {
                    "agent_id": decision.get("agent_id"),
                    "agent_type": decision.get("agent_type"),
                },
                "created_at": result.created_at.isoformat(),
            }
        )
    return events


def _task_payloads(store: RuntimeStore, run_id: str) -> list[dict[str, object]]:
    if not run_id:
        return []
    return [_task_payload(task) for task in store.list_mission_tasks(run_id=run_id, limit=100)]


def _task_payload(task: MissionTask) -> dict[str, object]:
    spec = task.agent_spec
    return {
        "task_id": task.task_id,
        "sequence": task.sequence,
        "title": task.title,
        "description": task.description,
        "status": task.status.value,
        "agent_id": spec.agent_id,
        "agent_type": spec.agent_type,
        "agent_purpose": spec.purpose,
        "capabilities": spec.capabilities,
        "tools_allowed": spec.tools_allowed,
        "memory_scope": spec.memory_scope,
        "constraints": spec.constraints,
        "interaction_points": spec.interaction_points,
        "risk_level": spec.risk_level.value,
        "provider_id": spec.provider_id,
        "model_id": spec.model_id,
        "architect_decision": task.architect_decision,
        "required_capabilities": task.required_capabilities,
        "expected_outputs": task.expected_outputs,
        "completion_checks": task.completion_checks,
        "user_actions": task.user_actions,
        "result_summary": task.result_summary,
        "tool_result_count": task.tool_result_count,
        "error": task.error,
    }


def _task_payloads_from_events(events: list[MissionStatusEvent]) -> list[dict[str, object]]:
    for event in reversed(events):
        raw_tasks = event.details.get("tasks")
        if not isinstance(raw_tasks, list):
            continue
        tasks = []
        for raw_task in raw_tasks:
            if isinstance(raw_task, dict):
                tasks.append({str(key): value for key, value in raw_task.items()})
        if tasks:
            return tasks
    return []


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_string_value(item) for item in value if _string_value(item)]


def _string_value(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _result_payload(result: MatrixRunResult | None) -> dict[str, object] | None:
    if result is None:
        return None
    return {
        "run_id": result.run_id,
        "response": result.response,
        "completed_count": result.metadata.get("mission_completed_count"),
        "task_count": result.metadata.get("mission_task_count"),
    }


def _run_status(result: MatrixRunResult) -> str:
    task_count = result.metadata.get("mission_task_count")
    completed_count = result.metadata.get("mission_completed_count")
    if task_count and completed_count == task_count:
        return "completed"
    if result.metadata.get("agent_execution_status") == "error":
        return "failed"
    return "recorded"


def _mission_status_script(
    status_url: str,
    payload: dict[str, object],
    agent_action_base_url: str,
    approval_action_url: str,
) -> str:
    return f"""
  <script>
    (function () {{
      const statusUrl = {json.dumps(status_url)};
      const agentActionBaseUrl = {json.dumps(agent_action_base_url)};
      const approvalActionUrl = {json.dumps(approval_action_url)};
      let payload = {_script_json(payload)};
      const primaryAction = document.getElementById('primary-action-link');
      const state = document.getElementById('mission-state');
      const message = document.getElementById('mission-message');
      const request = document.getElementById('mission-request');
      const events = document.getElementById('mission-events');
      const tasks = document.getElementById('mission-tasks');
      const result = document.getElementById('mission-result');
      const contract = document.getElementById('mission-contract');
      const approvals = document.getElementById('mission-approvals');

      function clear(node) {{
        while (node && node.firstChild) node.removeChild(node.firstChild);
      }}

      function line(title, body, tag) {{
        const item = document.createElement('div');
        item.className = 'timeline-item';
        const top = document.createElement('p');
        top.className = 'timeline-title';
        top.textContent = title;
        const meta = document.createElement('p');
        meta.className = 'muted';
        meta.textContent = body || '';
        item.appendChild(top);
        if (tag) {{
          const status = document.createElement('span');
          status.className = 'pill';
          status.textContent = tag;
          item.appendChild(status);
        }}
        item.appendChild(meta);
        return item;
      }}

      function values(items) {{
        if (!Array.isArray(items)) return [];
        return items.map((item) => String(item || '').trim()).filter(Boolean);
      }}

      function appendField(parent, label, value) {{
        const clean = String(value || '').trim();
        if (!clean) return;
        const item = document.createElement('div');
        item.className = 'contract-field';
        const key = document.createElement('p');
        key.className = 'kicker';
        key.textContent = label;
        const body = document.createElement('p');
        body.textContent = clean;
        item.appendChild(key);
        item.appendChild(body);
        parent.appendChild(item);
      }}

      function appendListField(parent, label, items, fallback) {{
        const clean = values(items);
        appendField(parent, label, clean.length ? clean.join('; ') : fallback);
      }}

      function taskProof(task) {{
        if (task.error) return 'Blocked or failed: ' + task.error;
        if (task.result_summary) return task.result_summary;
        if (task.status === 'skipped') return 'Waiting for a configured provider or attached runtime.';
        return 'No result recorded yet.';
      }}

      function taskStep(task) {{
        const item = document.createElement('div');
        item.className = 'execution-step';
        const head = document.createElement('div');
        head.className = 'execution-step-head';
        const title = document.createElement('p');
        title.className = 'timeline-title';
        title.textContent = (task.sequence || '?') + '. ' + (task.title || 'Task');
        head.appendChild(title);
        if (task.status) {{
          const status = document.createElement('span');
          status.className = 'pill';
          status.textContent = task.status;
          head.appendChild(status);
        }}
        item.appendChild(head);

        const objective = document.createElement('p');
        objective.className = 'request-text';
        objective.textContent = task.description || task.title || 'No task objective recorded.';
        item.appendChild(objective);

        const details = document.createElement('div');
        details.className = 'execution-details';
        appendField(
          details,
          'Agent',
          (task.agent_id || 'unknown') + ' / ' + (task.agent_type || 'unknown')
        );
        appendField(details, 'Purpose', task.agent_purpose);
        appendListField(
          details,
          'Required capabilities',
          task.required_capabilities || task.capabilities,
          'No capability list recorded.'
        );
        appendListField(details, 'Expected outputs', task.expected_outputs, 'No expected outputs recorded.');
        appendListField(details, 'Completion checks', task.completion_checks, 'No completion checks recorded.');
        appendListField(details, 'Allowed tools', task.tools_allowed, 'No tools recorded.');
        appendListField(
          details,
          'User actions',
          task.user_actions || task.interaction_points,
          'No user action recorded.'
        );
        appendListField(details, 'Guardrails', task.constraints, 'No task guardrails recorded.');
        appendField(details, 'Proof', taskProof(task));
        item.appendChild(details);
        return item;
      }}

      function renderContract() {{
        clear(contract);
        const context = payload.mission_context || {{}};
        const taskList = payload.tasks || [];
        const box = document.createElement('div');
        box.className = 'mission-contract';
        const heading = document.createElement('h2');
        heading.textContent = 'How This Mission Succeeds';
        box.appendChild(heading);

        const grid = document.createElement('div');
        grid.className = 'contract-grid';
        appendField(grid, 'Mission need', context.need || context.intent || payload.request);
        appendListField(grid, 'Success signals', context.success_criteria, 'No success signals recorded yet.');
        appendListField(grid, 'Mission guardrails', context.constraints, 'No extra guardrails recorded.');
        appendField(grid, 'Strategy', context.strategy);
        if (!grid.childNodes.length) {{
          appendField(grid, 'Plan', 'Waiting for Architect to publish the execution path.');
        }}
        box.appendChild(grid);

        const pathTitle = document.createElement('h3');
        pathTitle.textContent = 'Execution Path';
        box.appendChild(pathTitle);
        const path = document.createElement('div');
        path.className = 'execution-path';
        if (taskList.length) {{
          taskList.forEach((task) => path.appendChild(taskStep(task)));
        }} else {{
          path.appendChild(line('Plan pending', 'The planner has not published task mechanics yet.', 'pending'));
        }}
        box.appendChild(path);
        contract.appendChild(box);
      }}

      function agentUrl(agentId) {{
        return agentActionBaseUrl + '&agent_id=' + encodeURIComponent(agentId);
      }}

      function updatePrimaryAction() {{
        if (!primaryAction || !payload.agent_id || !payload.agent_available) return;
        primaryAction.href = agentUrl(payload.agent_id);
        primaryAction.textContent = 'Ask This Agent';
      }}

      async function answerApproval(approvalId, decision, button) {{
        if (button) button.disabled = true;
        const body = new URLSearchParams();
        body.set('approval_id', approvalId);
        body.set('decision', decision);
        try {{
          await fetch(approvalActionUrl, {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
            body: body.toString()
          }});
          await refresh();
        }} catch (error) {{
          if (button) button.disabled = false;
        }}
      }}

      function approvalCard(approval) {{
        const box = document.createElement('div');
        box.className = 'approval-card';
        const title = document.createElement('p');
        title.className = 'timeline-title';
        title.textContent = 'User Approval Needed';
        const target = document.createElement('p');
        target.className = 'request-text';
        target.textContent = approval.target || 'Unknown target';
        const reason = document.createElement('p');
        reason.className = 'muted';
        reason.textContent = approval.reason || 'The agent requested approval.';
        const purpose = document.createElement('p');
        purpose.className = 'muted';
        purpose.textContent = approval.purpose ? 'Purpose: ' + approval.purpose : '';
        const actions = document.createElement('div');
        actions.className = 'approval-actions';
        const approve = document.createElement('button');
        approve.type = 'button';
        approve.textContent = 'Approve';
        approve.addEventListener('click', () => answerApproval(approval.approval_id, 'approve', approve));
        const deny = document.createElement('button');
        deny.type = 'button';
        deny.textContent = 'Deny';
        deny.addEventListener('click', () => answerApproval(approval.approval_id, 'deny', deny));
        actions.appendChild(approve);
        actions.appendChild(deny);
        box.appendChild(title);
        box.appendChild(target);
        box.appendChild(reason);
        if (approval.purpose) box.appendChild(purpose);
        box.appendChild(actions);
        return box;
      }}

      function renderApprovals() {{
        clear(approvals);
        const pending = payload.approvals || [];
        if (!pending.length) return;
        const panel = document.createElement('div');
        panel.className = 'approval-panel';
        const heading = document.createElement('p');
        heading.className = 'timeline-title';
        heading.textContent = 'Waiting On You';
        const note = document.createElement('p');
        note.className = 'muted';
        note.textContent = 'This mission is paused until you approve or deny the pending request.';
        panel.appendChild(heading);
        panel.appendChild(note);
        pending.forEach((approval) => panel.appendChild(approvalCard(approval)));
        approvals.appendChild(panel);
      }}

      function render(next) {{
        payload = next;
        updatePrimaryAction();
        state.textContent = String(payload.status || 'unknown').toUpperCase();
        message.textContent = payload.message || '';
        request.textContent = payload.request || '';
        renderContract();
        renderApprovals();
        clear(events);
        (payload.events || []).forEach((event) => {{
          events.appendChild(line(event.stage || 'event', event.message || '', event.created_at || ''));
        }});
        if (!(payload.events || []).length) {{
          events.appendChild(line('waiting', 'The mission has been accepted.', 'now'));
        }}
        clear(tasks);
        (payload.tasks || []).forEach((task) => {{
          const taskBody = [
            task.description || '',
            'Agent: ' + (task.agent_id || 'unknown')
          ].filter(Boolean).join(' | ');
          tasks.appendChild(line(
            (task.sequence || '?') + '. ' + (task.title || 'Task'),
            taskBody,
            task.status || ''
          ));
        }});
        if (!(payload.tasks || []).length) {{
          tasks.appendChild(line('No task ledger yet', 'The planner has not produced visible tasks yet.', 'pending'));
        }}
        clear(result);
        if (payload.result) {{
          const box = document.createElement('div');
          box.className = 'notice';
          const title = document.createElement('strong');
          title.textContent = 'Mission Result';
          const run = document.createElement('p');
          run.innerHTML = 'Run <code></code>';
          run.querySelector('code').textContent = payload.result.run_id || '';
          const response = document.createElement('div');
          response.className = 'result';
          response.textContent = payload.result.response || '';
          box.appendChild(title);
          box.appendChild(run);
          box.appendChild(response);
          if (payload.agent_id && payload.agent_available) {{
            const actions = document.createElement('div');
            actions.className = 'result-actions';
            const agentLink = document.createElement('a');
            agentLink.className = 'button-link';
            agentLink.href = agentUrl(payload.agent_id);
            agentLink.textContent = 'Ask This Agent';
            actions.appendChild(agentLink);
            box.appendChild(actions);
          }}
          result.appendChild(box);
        }} else if (payload.error) {{
          const box = document.createElement('div');
          box.className = 'notice error';
          box.textContent = payload.error;
          result.appendChild(box);
        }}
      }}

      async function refresh() {{
        try {{
          const response = await fetch(statusUrl, {{ cache: 'no-store' }});
          if (response.ok) render(await response.json());
        }} catch (error) {{
          return;
        }}
        if (payload.status === 'queued' || payload.status === 'running') {{
          window.setTimeout(refresh, 1200);
        }}
      }}

      render(payload);
      if (payload.status === 'queued' || payload.status === 'running') {{
        window.setTimeout(refresh, 900);
      }}
    }})();
  </script>
"""


def _script_json(payload: dict[str, object]) -> str:
    return json.dumps(payload).replace("</", "<\\/")


def _agent_context_key(agent_id: str) -> str:
    return f"agent:{agent_id}"


def _clarification_context_from_form(form: dict[str, str]) -> str:
    context = form.get("context", "mission").strip()
    if context == "agent":
        agent_id = form.get("agent_id", "").strip()
        return _agent_context_key(agent_id) if agent_id else "agent:"
    return "mission"


def _default_target_for_context(context_key: str) -> str:
    if context_key.startswith("agent:") and context_key != "agent:":
        return context_key
    return "auto"


def _render_context_page(
    paths: MatrixPaths,
    store: RuntimeStore,
    token: str,
    context_key: str,
    agent_id: str,
    clarification_session: ClarificationSession,
    response: AppUiResponse,
) -> str:
    if context_key.startswith("agent:"):
        actual_agent_id = agent_id or context_key.split(":", 1)[1]
        return _agent_page(
            paths,
            store,
            token,
            actual_agent_id,
            response,
            clarification_session=clarification_session,
        )
    return render_app_page(
        paths,
        store,
        token,
        response,
        clarification_session=clarification_session,
    )


def _clarified_launch_request(
    clarifier: ClarificationService,
    draft: str,
    session: ClarificationSession,
) -> str:
    if not session.has_turns:
        return draft
    return clarifier.summarize(draft=draft, transcript=session.turns)


def _intake_pairs_from_form(form: dict[str, str]) -> list[tuple[str, str]]:
    """Rebuild (question, answer) pairs from a submitted intake form."""
    keys = [key.strip() for key in form.get("question_keys", "").split(",") if key.strip()]
    pairs: list[tuple[str, str]] = []
    for key in keys:
        question = form.get(f"qtext__{key}", "").strip()
        if not question:
            continue
        answer = form.get(f"ans__{key}", "").strip()
        if answer == "__other__":
            answer = form.get(f"other__{key}", "").strip()
        if not answer:
            answer = "(no preference - use your best judgment)"
        pairs.append((question, answer))
    return pairs


def _ensure_ready_or_ask(
    clarifier: ClarificationService,
    registry: ClarificationSessionRegistry,
    *,
    context_key: str,
    draft: str,
    target: str,
    default_target: str,
) -> tuple[ClarificationSession, bool]:
    session = registry.update_draft(context_key, draft, default_target)
    if _pending_clarification_question(session) is not None:
        return session, False
    if session.has_turns:
        return session, True
    try:
        session, _message = _ask_next_intent_question(
            clarifier,
            registry,
            context_key=context_key,
            draft=draft,
            target=target,
            default_target=default_target,
        )
    except Exception:
        return session, True
    return session, _pending_clarification_question(session) is None


def _pending_clarification_question(session: ClarificationSession) -> ClarificationTurn | None:
    if not session.turns:
        return None
    latest = session.turns[-1]
    if latest.role == ClarificationRole.ASSISTANT and latest.kind == "system_question":
        return latest
    return None


def _is_ready_clarification(value: str) -> bool:
    return value.strip().upper().startswith("READY")


def _ask_next_intent_question(
    clarifier: ClarificationService,
    registry: ClarificationSessionRegistry,
    *,
    context_key: str,
    draft: str,
    target: str,
    default_target: str,
) -> tuple[ClarificationSession, str]:
    session = registry.update_draft(context_key, draft, default_target)
    if _pending_clarification_question(session) is not None:
        return session, "Answer the current Matrix question before asking for another one."
    clarification = clarifier.ask_next(
        draft=draft,
        target=target,
        transcript=session.turns,
    )
    question = clarification.answer.strip()
    if _is_ready_clarification(question):
        return session, "The Matrix has enough context. You can run this mission now."
    if not question:
        question = "What outcome should this agent produce, and what boundaries should it follow?"
    updated = registry.append_system_question(
        context_key,
        draft=draft,
        target=clarification.target,
        question=question,
        default_target=clarification.target,
    )
    return updated, "The Matrix asked the next intent question."


def render_app_page(
    paths: MatrixPaths,
    store: RuntimeStore,
    token: str,
    response: AppUiResponse | None = None,
    clarification_session: ClarificationSession | None = None,
    pending_actions: list[dict[str, str]] | None = None,
    intake_questions: list[ClarifyingQuestion] | None = None,
) -> str:
    response = response or AppUiResponse()
    clarification_session = clarification_session or ClarificationSession(context_key="mission")
    provider_config = store.get_default_provider_config()
    provider_label = "unconfigured"
    if provider_config is not None:
        provider_label = f"{provider_config.provider_id} / {provider_config.selected_model}"
        if provider_config.reasoning_effort:
            provider_label += f" / {provider_config.reasoning_effort}"
    result_html = _result_panel(response)
    clarification_html = _clarification_composer(
        store,
        token,
        session=clarification_session,
        context="mission",
        draft_name="request",
        run_action=f"/ask?token={escape(token)}",
        run_label="Run Mission",
        running_label="Mission Running",
        draft_label="What do you want the agents to do?",
        draft_placeholder="Create a reusable research agent for comparing AI tools",
        submit_hint="Mission accepted. Keep this tab open.",
        intake_questions=intake_questions,
    )
    recent_html = _recent_runs_panel(
        store,
        token,
        pending_actions=pending_actions or _pending_clarification_actions(
            [clarification_session],
            token,
        ),
    )
    help_html = _help_panel()
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>The Matrix App</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=VT323&display=swap" rel="stylesheet">
  <style>
    :root {{
      color-scheme: dark;
      --void: #000000;
      --panel-bg: rgba(0, 14, 4, 0.82);
      --panel-edge: rgba(0, 255, 65, 0.14);
      --phosphor: #00b341;
      --phosphor-title: #7cff9d;
      --phosphor-bright: #00ff41;
      --phosphor-dim: #1f5530;
      --phosphor-red: #ff003c;
      --line: rgba(0, 255, 65, 0.14);
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; }}
    body {{
      background: var(--void);
      color: var(--phosphor);
      font-family: "Share Tech Mono", "Cascadia Mono", "Courier New", monospace;
      font-size: 15px;
      line-height: 1.6;
      min-height: 100vh;
      overflow-x: hidden;
    }}
    #matrix-rain {{
      position: fixed;
      inset: 0;
      width: 100vw;
      height: 100vh;
      z-index: 0;
      opacity: 0.42;
      pointer-events: none;
    }}
    body::before {{
      content: '';
      position: fixed;
      inset: 0;
      background: radial-gradient(ellipse 95% 75% at 50% 45%, transparent 0%, rgba(0,0,0,0.25) 62%, rgba(0,0,0,0.72) 100%);
      pointer-events: none;
      z-index: 1;
    }}
    body::after {{
      content: '';
      position: fixed;
      inset: 0;
      background: repeating-linear-gradient(0deg, transparent 0px, transparent 2px, rgba(0,0,0,0.22) 3px, transparent 4px);
      pointer-events: none;
      z-index: 999;
      mix-blend-mode: multiply;
    }}
    @keyframes blink {{ 0%, 49% {{ opacity: 1; }} 50%, 100% {{ opacity: 0; }} }}
    @keyframes wake {{
      from {{ opacity: 0; transform: translateY(8px); filter: blur(4px); }}
      to {{ opacity: 1; transform: translateY(0); filter: blur(0); }}
    }}
    main {{
      position: relative;
      z-index: 2;
      width: min(1040px, calc(100% - 48px));
      margin: 0 auto;
      padding: 36px 0 56px;
    }}
    header {{
      border-bottom: 1px solid var(--line);
      margin-bottom: 24px;
      padding-bottom: 20px;
      animation: wake 700ms ease-out both;
    }}
    h1 {{
      font-family: "VT323", "Cascadia Mono", monospace;
      font-size: clamp(58px, 8vw, 104px);
      line-height: 0.86;
      margin: 0 0 14px;
      color: var(--phosphor-bright);
      letter-spacing: 6px;
      text-transform: uppercase;
      text-shadow: 0 0 6px rgba(0,255,65,0.95), 0 0 28px rgba(0,255,65,0.45);
    }}
    h1::before {{ content: '> '; color: var(--phosphor-title); letter-spacing: 0; }}
    h1::after {{ content: '_'; animation: blink 1.05s step-end infinite; }}
    .hud {{
      display: flex;
      flex-wrap: wrap;
      gap: 18px;
      color: var(--phosphor-title);
      font-size: 12px;
      letter-spacing: 1.5px;
      text-transform: uppercase;
    }}
    .hud strong {{ color: var(--phosphor-bright); font-weight: normal; }}
    .panel {{
      position: relative;
      background: var(--panel-bg);
      border: 1px solid var(--panel-edge);
      border-left: 2px solid var(--phosphor-dim);
      padding: 28px 22px 22px;
      margin: 18px 0;
      animation: wake 700ms ease-out both;
    }}
    .panel::before {{
      content: '◤';
      position: absolute;
      top: 6px;
      left: 8px;
      color: var(--phosphor-title);
      font-size: 10px;
    }}
    h2 {{
      margin: 0 0 16px;
      color: var(--phosphor-title);
      font-size: 13px;
      font-weight: normal;
      letter-spacing: 2.5px;
      text-transform: uppercase;
      border-bottom: 1px dashed var(--line);
      padding-bottom: 8px;
    }}
    h2::before {{ content: '// '; }}
    label {{
      display: grid;
      gap: 10px;
      color: var(--phosphor-title);
      font-size: 12px;
      letter-spacing: 2px;
      text-transform: uppercase;
    }}
    textarea {{
      width: 100%;
      min-height: 150px;
      resize: vertical;
      border: 1px solid var(--line);
      background: rgba(0, 8, 2, 0.86);
      color: var(--phosphor-bright);
      caret-color: var(--phosphor-bright);
      font: inherit;
      font-size: 15px;
      line-height: 1.6;
      padding: 14px;
      outline: none;
    }}
    textarea:focus {{
      border-color: var(--phosphor-bright);
      box-shadow: 0 0 0 1px var(--phosphor-bright), 0 0 16px rgba(0,255,65,0.25);
    }}
    input, select {{
      width: 100%;
      border: 1px solid var(--line);
      background: rgba(0, 8, 2, 0.86);
      color: var(--phosphor-bright);
      font: inherit;
      padding: 12px;
      outline: none;
    }}
    input:focus, select:focus {{
      border-color: var(--phosphor-bright);
      box-shadow: 0 0 0 1px var(--phosphor-bright), 0 0 16px rgba(0,255,65,0.25);
    }}
    button, .button-link {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      border: 1px solid var(--phosphor-bright);
      background: transparent;
      color: var(--phosphor-bright);
      cursor: pointer;
      font: inherit;
      font-family: "VT323", "Cascadia Mono", monospace;
      font-size: 22px;
      letter-spacing: 4px;
      margin-top: 18px;
      padding: 12px 24px;
      text-decoration: none;
      text-transform: uppercase;
      text-shadow: 0 0 8px rgba(0,255,65,0.7);
    }}
    button::before, .button-link::before {{ content: '▸'; font-size: 18px; }}
    button:hover, .button-link:hover {{ background: rgba(0,255,65,0.1); }}
    button:disabled {{
      cursor: wait;
      opacity: 0.68;
      border-color: var(--phosphor-title);
      color: var(--phosphor-title);
    }}
    .submit-status {{
      color: var(--phosphor-title);
      font-size: 12px;
      letter-spacing: 1.4px;
      text-transform: uppercase;
    }}
    .submit-status::after {{
      content: ' _';
      animation: blink 1.05s step-end infinite;
    }}
    .result {{
      white-space: pre-wrap;
      color: var(--phosphor-bright);
      text-shadow: 0 0 5px rgba(0,255,65,0.3);
    }}
    .error {{ color: var(--phosphor-red); }}
    .muted {{ color: var(--phosphor-title); }}
    .list {{ display: grid; gap: 12px; }}
    .item {{
      border-top: 1px dashed var(--line);
      padding-top: 12px;
    }}
    .item:first-child {{ border-top: 0; padding-top: 0; }}
    .run-link {{
      color: var(--phosphor-bright);
      text-decoration: none;
    }}
    .run-link:hover, .run-link:focus {{ text-decoration: none; }}
    .operator-row {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      align-items: start;
      border-top: 1px dashed var(--line);
      padding-top: 12px;
    }}
    .operator-row:first-child {{ border-top: 0; padding-top: 0; }}
    .operator-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
    }}
    .operator-actions button {{ font-size: 18px; letter-spacing: 2px; margin-top: 0; padding: 8px 12px; }}
    details.panel summary {{
      cursor: pointer;
      list-style: none;
      color: var(--phosphor-bright);
      font-size: 15px;
      letter-spacing: 2px;
      text-transform: uppercase;
      text-shadow: 0 0 6px rgba(0,255,65,0.4);
    }}
    details.panel summary::-webkit-details-marker {{ display: none; }}
    details.panel summary::before {{ content: '▸ '; color: var(--phosphor-title); }}
    details.panel[open] summary::before {{ content: '▾ '; }}
    .help-list {{ margin-top: 16px; }}
    strong {{ color: var(--phosphor-bright); font-weight: normal; }}
    code {{ color: var(--phosphor-bright); overflow-wrap: anywhere; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }}
    .clarify-grid {{ display: grid; gap: 14px; }}
    .clarify-row {{ display: grid; grid-template-columns: minmax(180px, 260px) 1fr; gap: 14px; align-items: end; margin-top: 14px; }}
    .clarify-box {{
      border-top: 1px dashed var(--line);
      margin-top: 18px;
      padding-top: 16px;
    }}
    .clarify-box summary {{
      cursor: pointer;
      color: var(--phosphor-bright);
      letter-spacing: 2px;
      text-transform: uppercase;
    }}
    .clarify-box summary::before {{ content: 'â–¸ '; color: var(--phosphor-title); }}
    .clarify-box[open] summary::before {{ content: 'â–¾ '; }}
    .intent-check {{
      border-top: 1px dashed var(--line);
      margin-top: 18px;
      padding-top: 16px;
      display: grid;
      gap: 12px;
    }}
    .intent-check-head {{ display: grid; gap: 4px; }}
    .intent-check-head strong {{
      color: var(--phosphor-bright);
      letter-spacing: 2px;
      text-transform: uppercase;
    }}
    .intent-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: center;
    }}
    .intent-actions button {{ margin-top: 0; }}
    .intent-answer {{ display: grid; gap: 12px; }}
    .clarification-popup {{
      position: fixed;
      inset: 0;
      z-index: 1200;
      display: grid;
      place-items: center;
      padding: 24px;
      background: rgba(0, 0, 0, 0.74);
    }}
    .clarification-dialog {{
      width: min(720px, 100%);
      max-height: min(720px, calc(100vh - 48px));
      overflow: auto;
      border: 1px solid var(--phosphor-bright);
      border-left: 2px solid var(--phosphor-bright);
      background: rgba(0, 14, 4, 0.96);
      box-shadow: 0 0 0 1px rgba(0,255,65,0.18), 0 0 34px rgba(0,255,65,0.22);
      padding: 22px;
    }}
    .clarification-dialog h3 {{
      margin: 0 0 12px;
      color: var(--phosphor-bright);
      font-size: 16px;
      font-weight: normal;
      letter-spacing: 2px;
      text-transform: uppercase;
    }}
    .clarification-question {{
      color: var(--phosphor-bright);
      font-size: 16px;
      margin-bottom: 18px;
      white-space: pre-wrap;
    }}
    .clarification-inline-note {{
      border: 1px solid rgba(0,255,65,0.26);
      padding: 12px;
    }}
    .intake-dialog {{
      display: flex;
      flex-direction: column;
      gap: 14px;
      overflow: hidden;
    }}
    .intake-header {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .intake-header h3 {{ margin: 0; }}
    .intake-progress {{
      color: var(--phosphor-title);
      font-size: 12px;
      letter-spacing: 1.5px;
      text-transform: uppercase;
      white-space: nowrap;
    }}
    .intake-body {{
      display: grid;
      gap: 16px;
      overflow: auto;
      padding-right: 4px;
    }}
    .intake-question {{
      border: 0;
      border-top: 1px dashed var(--line);
      margin: 0;
      padding: 14px 0 0;
    }}
    .intake-question legend {{
      color: var(--phosphor-bright);
      font-size: 15px;
      padding: 0;
    }}
    .intake-why {{ margin: 4px 0 10px; font-size: 13px; }}
    .intake-options {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .option-chip {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid rgba(0,255,65,0.32);
      padding: 6px 12px;
      cursor: pointer;
      user-select: none;
    }}
    .option-chip input {{ accent-color: var(--phosphor-bright); margin: 0; }}
    .option-chip:has(input:checked) {{
      border-color: var(--phosphor-bright);
      background: rgba(0,255,65,0.12);
      box-shadow: 0 0 14px rgba(0,255,65,0.18);
    }}
    .chip-rec {{
      color: var(--phosphor-title);
      font-size: 10px;
      letter-spacing: 1px;
      text-transform: uppercase;
      font-style: normal;
    }}
    .intake-text {{
      width: 100%;
      margin-top: 8px;
      background: rgba(0,0,0,0.4);
      border: 1px solid rgba(0,255,65,0.32);
      color: var(--phosphor-bright);
      padding: 8px 10px;
      font: inherit;
    }}
    .intake-footer {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      border-top: 1px solid rgba(0,255,65,0.26);
      padding-top: 12px;
    }}
    .intake-footer button {{ margin: 0; cursor: pointer; }}
    .intake-start[disabled] {{ opacity: 0.45; cursor: not-allowed; }}
    .intake-defaults, .intake-edit {{
      background: transparent;
      border: 1px solid rgba(0,255,65,0.32);
      color: var(--phosphor);
    }}
    .intake-edit {{ margin-left: auto; }}
    .transcript {{ display: grid; gap: 10px; }}
    .turn {{
      border-top: 1px dashed var(--line);
      padding-top: 10px;
    }}
    .turn:first-child {{ border-top: 0; padding-top: 0; }}
    .turn-label {{
      color: var(--phosphor-title);
      font-size: 12px;
      letter-spacing: 1.5px;
      text-transform: uppercase;
    }}
    @media (max-width: 760px) {{
      main {{ width: min(1040px, calc(100% - 28px)); }}
      h1 {{ font-size: 54px; letter-spacing: 3px; }}
      .clarify-row {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <canvas id="matrix-rain" aria-hidden="true"></canvas>
  <main>
    <header>
      <h1>The Matrix</h1>
      <div class="hud">
        <span>app<strong>::local</strong></span>
        <span>provider<strong>::{escape(provider_label)}</strong></span>
        <span>vault<strong>::{escape(str(paths.vault))}</strong></span>
      </div>
    </header>
    {clarification_html}
    {result_html}
    {help_html}
    {recent_html}
  </main>
  <script>
    (function () {{
      const canvas = document.getElementById('matrix-rain');
      if (!canvas || !canvas.getContext) return;
      const ctx = canvas.getContext('2d');
      const glyphs = 'ﾊﾐﾋｰｳｼﾅﾓﾆｻﾜﾂｵﾘｱﾎﾃﾏｹﾒｴｶｷﾑﾕﾗｾﾈｽﾀﾇﾍ0123456789ABCDEF<>/\\\\|=+-*';
      const fontSize = 16;
      let cols, drops;
      function setup() {{
        canvas.width = window.innerWidth;
        canvas.height = window.innerHeight;
        cols = Math.floor(canvas.width / fontSize);
        drops = Array.from({{ length: cols }}, () => Math.random() * -80);
      }}
      setup();
      window.addEventListener('resize', setup);
      function draw() {{
        ctx.fillStyle = 'rgba(0, 0, 0, 0.045)';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.font = fontSize + 'px "Share Tech Mono", "Cascadia Mono", monospace';
        for (let i = 0; i < cols; i++) {{
          const ch = glyphs.charAt(Math.floor(Math.random() * glyphs.length));
          const y = drops[i] * fontSize;
          ctx.fillStyle = Math.random() > 0.975 ? '#d4ffe2' : '#00b341';
          ctx.fillText(ch, i * fontSize, y);
          if (y > canvas.height && Math.random() > 0.972) drops[i] = 0;
          drops[i]++;
        }}
      }}
      setInterval(draw, 60);
    }})();
  </script>
  {_submit_feedback_script()}
</body>
</html>
"""


def _clarification_composer(
    store: RuntimeStore,
    token: str,
    *,
    session: ClarificationSession,
    context: str,
    draft_name: str,
    run_action: str,
    run_label: str,
    running_label: str,
    draft_label: str,
    draft_placeholder: str,
    submit_hint: str,
    agent_id: str = "",
    disabled: bool = False,
    intake_questions: list[ClarifyingQuestion] | None = None,
) -> str:
    draft = session.draft
    hidden_agent = (
        f'<input type="hidden" name="agent_id" value="{escape(agent_id, quote=True)}">'
        if agent_id
        else ""
    )
    if intake_questions:
        intent_html = _intake_form(
            token,
            context=context,
            draft=draft,
            questions=intake_questions,
            hidden_agent=hidden_agent,
        )
    else:
        intent_html = _intent_clarification_controls(
            token,
            session=session,
            context=context,
            draft=draft,
            hidden_agent=hidden_agent,
        )
    disabled_attr = " disabled" if disabled else ""
    heading = "Mission Console" if context == "mission" else "Agent Console"
    return f"""
    <section class="panel">
      <h2>{heading}</h2>
      <div class="clarify-grid">
        <form method="post" action="{run_action}" data-mission-form>
          {hidden_agent}
          <label>{escape(draft_label)}
            <textarea name="{escape(draft_name, quote=True)}" placeholder="{escape(draft_placeholder, quote=True)}" required data-clarify-draft>{escape(draft)}</textarea>
          </label>
          <div class="actions">
            <button type="submit" data-running-label="{escape(running_label, quote=True)}"{disabled_attr}>{escape(run_label)}</button>
            <a class="button-link" href="/dashboard?token={escape(token)}">Back to Dashboard</a>
            <a class="button-link" href="/settings?token={escape(token)}">Provider Settings</a>
            <span class="submit-status" hidden aria-live="polite">
              {escape(submit_hint)}
            </span>
          </div>
        </form>
        {intent_html}
      </div>
    </section>
"""


def _intent_clarification_controls(
    token: str,
    *,
    session: ClarificationSession,
    context: str,
    draft: str,
    hidden_agent: str,
) -> str:
    answer_action = f"/clarify/answer?token={escape(token, quote=True)}"
    reset_action = f"/clarify/reset?token={escape(token, quote=True)}"
    target = session.default_target or _default_target_for_context(session.context_key)
    hidden_fields = (
        f'<input type="hidden" name="context" value="{escape(context, quote=True)}">'
        f'<input type="hidden" name="draft" value="{escape(draft, quote=True)}" data-draft-sync>'
        f'<input type="hidden" name="target" value="{escape(target, quote=True)}">'
        f"{hidden_agent}"
    )
    pending = _pending_clarification_question(session)
    if pending is None:
        active_form = """
          <p class="muted">Click Run Mission. If the brief is missing an important detail, The Matrix will ask before anything starts.</p>
"""
    else:
        reset_button = f"""
              <form method="post" action="{reset_action}">
                <input type="hidden" name="context" value="{escape(context, quote=True)}">
                {hidden_agent}
                <button type="submit">Edit Brief Instead</button>
              </form>
"""
        active_form = f"""
          <div class="clarification-inline-note">
            <strong>Clarification needed</strong>
            <p class="muted">The Matrix is waiting for your answer before it starts.</p>
          </div>
          <div class="clarification-popup" role="dialog" aria-modal="true" aria-labelledby="clarification-title" data-clarification-popup>
            <div class="clarification-dialog">
              <h3 id="clarification-title">Clarification Required</h3>
              <p class="muted">The mission is paused until this is answered.</p>
              <div class="clarification-question">{escape(pending.content)}</div>
              <form method="post" action="{answer_action}" class="intent-answer">
                {hidden_fields}
                <label>Your Answer
                  <input name="answer" placeholder="Answer the Matrix question" required>
                </label>
                <div class="intent-actions">
                  <button type="submit">Save Answer</button>
                </div>
              </form>
              <div class="intent-actions">
                {reset_button}
              </div>
            </div>
          </div>
"""
    transcript_heading = "Intent Transcript"
    transcript = _clarification_transcript_html(session.turns)
    reset_form = ""
    if session.turns:
        reset_form = f"""
          <form method="post" action="{reset_action}">
            <input type="hidden" name="context" value="{escape(context, quote=True)}">
            {hidden_agent}
            <div class="intent-actions">
              <button type="submit">Reset Intent</button>
            </div>
          </form>
"""
    return f"""
        <div class="intent-check">
          <div class="intent-check-head">
            <strong>Intent Check</strong>
            <p class="muted">The Matrix can ask what it needs before building or running the agent.</p>
          </div>
          {active_form}
          <div class="notice">
            <strong>{transcript_heading}</strong>
            {transcript}
          </div>
          {reset_form}
        </div>
"""


def _intake_field_key(question_id: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in question_id.strip())
    return cleaned or "q"


def _intake_question_block(index: int, question: ClarifyingQuestion) -> str:
    key = _intake_field_key(question.id)
    why = (
        f'<p class="muted intake-why">{escape(question.why)}</p>' if question.why else ""
    )
    hidden_text = (
        f'<input type="hidden" name="qtext__{escape(key, quote=True)}" '
        f'value="{escape(question.question, quote=True)}">'
    )
    if not question.options:
        body = (
            '<input type="text" class="intake-text" '
            f'name="ans__{escape(key, quote=True)}" '
            'placeholder="Your answer" data-intake-input>'
        )
    else:
        checked_value = (
            question.recommended
            if question.recommended in question.options
            else question.options[0]
        )
        chips = []
        for option in question.options:
            checked = " checked" if option == checked_value else ""
            recommended_tag = (
                ' <em class="chip-rec">recommended</em>'
                if option == checked_value
                else ""
            )
            chips.append(
                '<label class="option-chip">'
                f'<input type="radio" name="ans__{escape(key, quote=True)}" '
                f'value="{escape(option, quote=True)}"{checked} data-intake-input>'
                f"<span>{escape(option)}{recommended_tag}</span>"
                "</label>"
            )
        chips.append(
            '<label class="option-chip option-other">'
            f'<input type="radio" name="ans__{escape(key, quote=True)}" '
            'value="__other__" data-intake-input data-intake-other>'
            "<span>Other&hellip;</span>"
            "</label>"
        )
        other_input = (
            '<input type="text" class="intake-text intake-other-input" '
            f'name="other__{escape(key, quote=True)}" '
            'placeholder="Type your own answer" hidden>'
        )
        body = f'<div class="intake-options">{"".join(chips)}</div>{other_input}'
    return f"""
            <fieldset class="intake-question" data-intake-question>
              <legend>{index}. {escape(question.question)}</legend>
              {why}
              {hidden_text}
              {body}
            </fieldset>
"""


def _intake_form(
    token: str,
    *,
    context: str,
    draft: str,
    questions: list[ClarifyingQuestion],
    hidden_agent: str,
) -> str:
    submit_action = f"/intake/submit?token={escape(token, quote=True)}"
    reset_action = f"/clarify/reset?token={escape(token, quote=True)}"
    keys = ",".join(_intake_field_key(question.id) for question in questions)
    blocks = "".join(
        _intake_question_block(index, question)
        for index, question in enumerate(questions, start=1)
    )
    total = len(questions)
    return f"""
        <div class="intent-check">
          <div class="intent-check-head">
            <strong>Mission Intake</strong>
            <p class="muted">Answer these before the agents are spawned so they can run on their own.</p>
          </div>
          <div class="clarification-popup" role="dialog" aria-modal="true" aria-labelledby="intake-title" data-intake-popup>
            <form class="clarification-dialog intake-dialog" method="post" action="{submit_action}" data-intake-form>
              <div class="intake-header">
                <h3 id="intake-title">Before The Matrix Spawns Agents</h3>
                <span class="intake-progress" data-intake-progress>{total} of {total} answered</span>
              </div>
              <p class="muted">Defaults are pre-selected. Adjust anything, or accept the recommended set.</p>
              <input type="hidden" name="context" value="{escape(context, quote=True)}">
              <input type="hidden" name="request" value="{escape(draft, quote=True)}">
              <input type="hidden" name="question_keys" value="{escape(keys, quote=True)}">
              {hidden_agent}
              <div class="intake-body">
                {blocks}
              </div>
              <div class="intake-footer">
                <button type="submit" class="intake-start" data-intake-start>Start Mission</button>
                <button type="submit" class="intake-defaults" data-intake-defaults formnovalidate>Accept Recommended Defaults</button>
                <button type="submit" class="intake-edit" formaction="{reset_action}" formnovalidate>Edit Brief Instead</button>
              </div>
            </form>
          </div>
        </div>
"""


def _pending_clarification_actions(
    sessions: list[ClarificationSession],
    token: str,
) -> list[dict[str, str]]:
    actions = []
    for session in sessions:
        pending = _pending_clarification_question(session)
        if pending is None:
            continue
        context = session.context_key
        if context == "oracle":
            continue
        if context.startswith("agent:") and context != "agent:":
            agent_id = context.split(":", 1)[1]
            href = _token_url("/agent", token, agent_id=agent_id)
            title = f"Clarification needed for {agent_id}"
        else:
            href = _token_url("/", token)
            title = "Clarification needed for mission"
        actions.append(
            {
                "kind": "clarification",
                "title": title,
                "href": href,
                "status": "needs clarification",
                "body": pending.content,
            }
        )
    return actions


def _pending_user_actions(
    clarification_registry: ClarificationSessionRegistry,
    approval_registry: ApprovalRegistry,
    token: str,
) -> list[dict[str, str]]:
    sessions = clarification_registry.list_sessions()
    actions = _pending_clarification_actions(sessions, token)
    for approval in approval_registry.pending_all_payloads():
        job_id = str(approval.get("job_id") or "")
        href = _token_url("/mission", token, job_id=job_id) if job_id else _token_url("/", token)
        actions.append(
            {
                "kind": "approval",
                "title": "Approval needed",
                "href": href,
                "status": "needs approval",
                "body": str(approval.get("target") or approval.get("reason") or ""),
                "approval_id": str(approval.get("approval_id") or ""),
                "reason": str(approval.get("reason") or ""),
                "purpose": str(approval.get("purpose") or ""),
            }
        )
    return actions


def _clarification_target_options(
    store: RuntimeStore,
    selected: str,
    *,
    agent_id: str = "",
) -> str:
    options = [
        ("auto", "Auto / The Matrix"),
        ("matrix", "The Matrix"),
        ("oracle", "Oracle"),
        ("architect", "Architect"),
        ("neo", "Neo"),
    ]
    seen = {value for value, _label in options}
    if agent_id:
        value = f"agent:{agent_id}"
        spec = store.get_agent(agent_id)
        label = f"Agent / {agent_id}" if spec is None else f"Agent / {spec.agent_id}"
        options.append((value, label))
        seen.add(value)
    for record in store.list_agent_records(limit=20):
        spec = store.get_agent(str(record["agent_id"]))
        if spec is None or not spec.reusable or not spec.enabled:
            continue
        value = f"agent:{spec.agent_id}"
        if value not in seen:
            options.append((value, f"Agent / {spec.agent_id}"))
            seen.add(value)
    return "".join(
        f'<option value="{escape(value, quote=True)}"{" selected" if value == selected else ""}>{escape(label)}</option>'
        for value, label in options
    )


def _clarification_transcript_html(turns: list[ClarificationTurn]) -> str:
    if not turns:
        return (
            '<p class="muted">No intent questions yet. Run starts only after the brief '
            "has enough context.</p>"
        )
    items = []
    for turn in turns:
        label = _clarification_turn_label(turn)
        items.append(
            f"""
          <div class="turn">
            <div class="turn-label">{escape(label)}</div>
            <div class="result">{escape(turn.content)}</div>
          </div>
"""
        )
    return f'<div class="transcript">{"".join(items)}</div>'


def _clarification_turn_label(turn: ClarificationTurn) -> str:
    target = f" / {turn.target}" if turn.target else ""
    if turn.kind == "system_question":
        return f"Matrix asks{target}"
    if turn.kind == "user_answer":
        return f"You answer{target}"
    if turn.kind == "user_question":
        return f"You ask{target}"
    if turn.kind == "assistant_answer":
        return f"Matrix answers{target}"
    speaker = "You" if turn.role == ClarificationRole.USER else "Matrix"
    return f"{speaker}{target}"


def _result_panel(response: AppUiResponse) -> str:
    if response.busy:
        return f"""
    <section class="panel">
      <h2>Mission Running</h2>
      <p class="muted">{escape(response.message or "A mission is already running.")}</p>
    </section>
"""
    if response.message:
        return f"""
    <section class="panel">
      <h2>System Message</h2>
      <p class="result">{escape(response.message)}</p>
    </section>
"""
    if response.error:
        return f"""
    <section class="panel">
      <h2>Mission Error</h2>
      <p class="error">{escape(response.error)}</p>
    </section>
"""
    if response.result is None:
        return ""
    result = response.result
    return f"""
    <section class="panel">
      <h2>Mission Result</h2>
      <p class="muted">Run <code>{escape(result.run_id)}</code></p>
      <div class="result">{escape(result.response)}</div>
    </section>
"""


def _oracle_page(
    token: str,
    session: ClarificationSession,
    response: AppUiResponse | None = None,
    oracle_job: OracleJob | None = None,
) -> str:
    response = response or AppUiResponse()
    stream_panel = ""
    extra_script = ""
    if oracle_job is not None:
        stream_panel = """
      <div class="oracle-stream" data-oracle-stream>
        <p class="kicker">Oracle Signal</p>
        <div class="oracle-scan" aria-hidden="true">
          <span></span><span></span><span></span>
        </div>
        <p class="muted" data-oracle-message>Receiving transmission...</p>
        <div class="result oracle-output" data-oracle-output></div>
      </div>
"""
        extra_script = _oracle_stream_script(
            f"/oracle/status?token={token}&job_id={oracle_job.job_id}"
        )
    content = f"""
      {_inline_response(response)}
      {stream_panel}
      <form method="post" action="/oracle/ask?token={escape(token, quote=True)}">
        <label>Question for the Oracle
          <textarea name="question" placeholder="Ask anything about the system, agents, strategy, risks, or next steps." required></textarea>
        </label>
        <div class="actions">
          <button type="submit">Ask Oracle</button>
        </div>
      </form>
      <div class="notice">
        <strong>Oracle Transcript</strong>
        {_clarification_transcript_html(session.turns)}
      </div>
"""
    return _utility_page(
        "Ask the Oracle",
        token,
        "Ask any read-only question. The Oracle answers, but does not launch missions.",
        content,
        primary_action_label="New Mission",
        primary_action_url=_token_url("/", token),
        extra_script=extra_script,
    )


def _oracle_stream_script(status_url: str) -> str:
    return f"""
  <script>
    (function () {{
      const statusUrl = {json.dumps(status_url)};
      const output = document.querySelector('[data-oracle-output]');
      const message = document.querySelector('[data-oracle-message]');
      if (!output || !message) return;
      const glyphs = '01AI<>/\\\\|=+-*THEMATRIXORACLE';
      let revealed = '';
      let answer = '';
      let done = false;

      function scrambleLine() {{
        if (done || revealed) return;
        let text = '';
        for (let i = 0; i < 54; i++) {{
          text += glyphs.charAt(Math.floor(Math.random() * glyphs.length));
        }}
        output.textContent = text;
      }}

      function typeAnswer(index) {{
        done = true;
        if (index === 0) output.textContent = '';
        if (index >= answer.length) {{
          output.textContent = answer;
          message.textContent = 'Oracle transmission complete.';
          return;
        }}
        revealed += answer.charAt(index);
        output.textContent = revealed + ' _';
        const delay = answer.charAt(index) === '\\n' ? 90 : 18 + Math.floor(Math.random() * 28);
        window.setTimeout(() => typeAnswer(index + 1), delay);
      }}

      async function refresh() {{
        try {{
          const response = await fetch(statusUrl);
          const payload = await response.json();
          if (!payload.found) {{
            message.textContent = 'Oracle signal lost.';
            done = true;
            return;
          }}
          message.textContent = payload.message || 'Receiving transmission...';
          if (payload.status === 'failed') {{
            done = true;
            output.textContent = payload.error || 'The Oracle could not answer this question.';
            return;
          }}
          if (payload.status === 'completed') {{
            answer = payload.answer || '';
            typeAnswer(0);
            return;
          }}
          window.setTimeout(refresh, 700);
        }} catch (error) {{
          message.textContent = 'Oracle signal interrupted. Retrying...';
          window.setTimeout(refresh, 900);
        }}
      }}

      window.setInterval(scrambleLine, 90);
      refresh();
    }})();
  </script>
"""


def _operator_panel(store: RuntimeStore, token: str) -> str:
    goals = _actionable_operator_goals(store.list_operator_goals(limit=20))[:5]
    items = []
    for goal in goals:
        next_run = goal.next_run_at.isoformat(timespec="seconds") if goal.next_run_at else "not scheduled"
        last = goal.last_result or "No runs recorded yet."
        if goal.status == OperatorGoalStatus.PENDING:
            last = "Waiting for your activation before anything recurring starts."
        actions = _operator_goal_actions(goal.goal_id, goal.status, token)
        items.append(
            f"""
        <div class="operator-row">
          <div>
            <p><a class="run-link" href="{_operator_goal_url(token, goal.goal_id)}"><strong>{escape(goal.title)}</strong></a></p>
            <p class="muted">status={escape(goal.status.value)} next={escape(next_run)}</p>
            <p class="muted">{escape(last)}</p>
          </div>
          <div class="operator-actions">{actions}</div>
        </div>
"""
        )
    content = "".join(items) or (
        '<p class="muted">No Operator goals need attention right now. Try: '
        'send me a notification to drink water every 5 minutes.</p>'
    )
    return f"""
    <section class="panel">
      <h2>The Operator</h2>
      <div class="list">{content}</div>
      <div class="actions">
        <a class="button-link" href="/operator?token={escape(token, quote=True)}">Open Operator</a>
      </div>
    </section>
"""


def _operator_goal_actions(goal_id: str, status: OperatorGoalStatus, token: str) -> str:
    action_url = f"/operator/action?token={escape(token, quote=True)}"
    if status == OperatorGoalStatus.PENDING:
        buttons = ["activate", "cancel"]
    elif status == OperatorGoalStatus.CANCELED:
        buttons = []
    elif status in {OperatorGoalStatus.COMPLETED, OperatorGoalStatus.FAILED}:
        buttons = ["cancel"]
    else:
        primary = "resume" if status == OperatorGoalStatus.PAUSED else "pause"
        buttons = [primary, "run_now", "cancel"]
    return "".join(
        f"""
            <form method="post" action="{action_url}">
              <input type="hidden" name="goal_id" value="{escape(goal_id, quote=True)}">
              <input type="hidden" name="action" value="{escape(action, quote=True)}">
              <button type="submit">{escape(action.replace("_", " "))}</button>
            </form>
"""
        for action in buttons
    )


def _operator_goal_url(token: str, goal_id: str) -> str:
    return escape(_token_url("/operator", token, goal_id=goal_id), quote=True)


def _actionable_operator_goals(goals) -> list:
    actionable = {
        OperatorGoalStatus.PENDING,
        OperatorGoalStatus.ACTIVE,
        OperatorGoalStatus.PAUSED,
        OperatorGoalStatus.FAILED,
    }
    return [goal for goal in goals if goal.status in actionable]


def _recent_runs_panel(
    store: RuntimeStore,
    token: str,
    pending_actions: list[dict[str, str]] | None = None,
) -> str:
    runs = store.list_run_records(limit=5)
    items = []
    for action in pending_actions or []:
        href = action["href"]
        title = action["title"]
        status = action["status"]
        body = action["body"]
        approval_controls = ""
        if action.get("kind") == "approval" and action.get("approval_id"):
            approval_controls = _approval_inline_forms(
                token,
                action["approval_id"],
                return_to="app",
            )
        items.append(
            f"""
        <div class="item">
          <p><a class="run-link" href="{escape(href, quote=True)}"><strong>{escape(title)}</strong></a></p>
          <p class="muted">{escape(status)}</p>
          <p class="muted">{escape(_clip(body, 180))}</p>
          {approval_controls}
        </div>
"""
        )
    for run in runs:
        href = f"/mission?token={escape(token, quote=True)}&run_id={escape(run['run_id'], quote=True)}"
        items.append(
            f"""
        <div class="item">
          <p><a class="run-link" href="{href}"><code>{escape(run["run_id"])}</code></a></p>
          <p class="muted">{escape(_clip(run["request"], 180))}</p>
        </div>
"""
        )
    content = "".join(items) or '<p class="muted">No missions recorded yet.</p>'
    return f"""
    <section class="panel">
      <h2>Recent Missions</h2>
      <div class="list">{content}</div>
    </section>
"""


def _approval_inline_forms(token: str, approval_id: str, *, return_to: str) -> str:
    action_url = f"/approval/respond?token={escape(token, quote=True)}"
    return f"""
          <div class="operator-actions">
            <form method="post" action="{action_url}">
              <input type="hidden" name="approval_id" value="{escape(approval_id, quote=True)}">
              <input type="hidden" name="decision" value="approve">
              <input type="hidden" name="return_to" value="{escape(return_to, quote=True)}">
              <button type="submit">Approve</button>
            </form>
            <form method="post" action="{action_url}">
              <input type="hidden" name="approval_id" value="{escape(approval_id, quote=True)}">
              <input type="hidden" name="decision" value="deny">
              <input type="hidden" name="return_to" value="{escape(return_to, quote=True)}">
              <button type="submit">Deny</button>
            </form>
          </div>
"""


def _help_panel() -> str:
    return """
    <details class="panel">
      <summary>Help / Commands</summary>
      <div class="list help-list">
        <div class="item">
          <p><strong>Run from browser</strong></p>
          <p class="muted">Use the request box above for normal agent missions. Ask The Matrix to check intent when the request needs shaping.</p>
        </div>
        <div class="item">
          <p><strong>Intent check</strong></p>
          <p class="muted">The Matrix asks the next useful question. Run uses a clean brief composed from the draft and answers.</p>
        </div>
        <div class="item">
          <p><strong>Approvals</strong></p>
          <p class="muted">When an agent needs approval during a mission, the Mission Status page shows Approve and Deny controls.</p>
        </div>
        <div class="item">
          <p><strong>The Operator</strong></p>
          <p class="muted">Recurring goals are drafted first. Review and activate them before they run, and keep this app open while they are scheduled.</p>
        </div>
        <div class="item">
          <p><strong>Change provider</strong></p>
          <p class="muted">Open Provider Settings in this page, choose provider/model, then save and test.</p>
        </div>
      </div>
    </details>
"""


def _update_agent_from_form(
    paths: MatrixPaths,
    vault: MemoryVault,
    store: RuntimeStore,
    form: dict[str, str],
):
    agent_id = form.get("agent_id", "").strip()
    if not agent_id:
        raise ValueError("Choose an agent before editing it.")
    spec = store.get_agent(agent_id)
    if spec is None:
        raise ValueError(f"No reusable agent exists with id `{agent_id}`.")

    purpose = " ".join(form.get("purpose", "").split())
    if not purpose:
        raise ValueError("Describe what this agent should do.")

    updated = spec.model_copy(
        update={
            "purpose": purpose[:300],
            "capabilities": _split_lines(form.get("capabilities", "")),
            "constraints": _split_lines(form.get("constraints", "")),
            "interaction_points": _split_lines(form.get("interaction_points", "")),
            "reusable": form.get("reusable") == "on",
            "enabled": form.get("enabled") == "on",
        }
    )
    return _save_agent_update(paths, vault, store, updated)


def _toggle_agent(
    paths: MatrixPaths,
    vault: MemoryVault,
    store: RuntimeStore,
    agent_id: str,
) -> AgentSpec:
    if not agent_id:
        raise ValueError("Choose an agent before changing its status.")
    spec = store.get_agent(agent_id)
    if spec is None:
        raise ValueError(f"No reusable agent exists with id `{agent_id}`.")
    updated = spec.model_copy(update={"enabled": not spec.enabled})
    return _save_agent_update(paths, vault, store, updated)


def _save_agent_update(
    paths: MatrixPaths,
    vault: MemoryVault,
    store: RuntimeStore,
    spec: AgentSpec,
) -> AgentSpec:
    prompt_library = PromptLibrary(paths.prompts_dir)
    prompt_ref = f"agent-blueprint-{spec.agent_id}"
    if prompt_ref not in spec.prompt_block_refs:
        spec = spec.model_copy(update={"prompt_block_refs": [*spec.prompt_block_refs, prompt_ref]})
    blueprint = _render_agent_blueprint(spec)
    prompt_library.write_agent_blueprint(spec.agent_id, blueprint)
    store.upsert_agent(spec)
    store.record_prompt_block(
        block_ref=prompt_ref,
        block_type="agent_blueprint",
        content=blueprint,
    )
    vault.record_agent_spec(spec)
    return spec


def _render_agent_blueprint(spec) -> str:
    return (
        f"# Agent Blueprint: {spec.agent_id}\n\n"
        f"You are a `{spec.agent_type}` sub-agent in The Matrix Agent System.\n\n"
        f"## Purpose\n\n{spec.purpose}\n\n"
        "## Operating Rules\n\n"
        "- Stay inside the stated purpose.\n"
        "- Use only the tools listed in this blueprint.\n"
        "- Do not read or reveal raw secrets.\n"
        "- Ask for user confirmation at the listed interaction points.\n"
        "- Keep final answers clear, simple, and concise.\n\n"
        f"## Capabilities\n\n{_markdown_list(spec.capabilities)}\n\n"
        f"## Tools Allowed\n\n{_markdown_list(spec.tools_allowed)}\n\n"
        f"## Memory Scope\n\n{_markdown_list(spec.memory_scope)}\n\n"
        f"## Constraints\n\n{_markdown_list(spec.constraints)}\n\n"
        f"## Interaction Points\n\n{_markdown_list(spec.interaction_points)}\n\n"
        f"## Risk Level\n\n{spec.risk_level.value}\n\n"
        f"## Enabled\n\n{spec.enabled}\n"
    )


def _split_lines(value: str) -> list[str]:
    items = []
    for line in value.replace(",", "\n").splitlines():
        item = " ".join(line.split())
        if item and item not in items:
            items.append(item[:160])
    return items[:12]


def _markdown_list(values: list[str]) -> str:
    if not values:
        return "- None"
    return "\n".join(f"- {value}" for value in values)


def _textarea_lines(values: list[str]) -> str:
    return "\n".join(values)


def _agent_page(
    paths: MatrixPaths,
    store: RuntimeStore,
    token: str,
    agent_id: str,
    response: AppUiResponse | None = None,
    clarification_session: ClarificationSession | None = None,
) -> str:
    spec = store.get_agent(agent_id)
    if spec is None:
        return _utility_page(
            "Agent Not Found",
            token,
            f"No reusable agent exists with id `{agent_id}`.",
            "",
        )

    tools = ", ".join(spec.tools_allowed) or "none"
    memory = ", ".join(spec.memory_scope) or "none"
    rows = _rows_html(
        [
            ("Agent", spec.agent_id, ""),
            ("Type", spec.agent_type, ""),
            ("Purpose", spec.purpose, ""),
            ("Risk", spec.risk_level.value, ""),
            ("Status", "active" if spec.enabled else "paused", ""),
            ("Reusable", "yes" if spec.reusable else "no", ""),
            ("Provider", spec.provider_id, spec.model_id),
            ("Tools", tools, ""),
            ("Memory", memory, ""),
        ]
    )
    action = f"/agent/run?token={escape(token, quote=True)}"
    update_action = f"/agent/update?token={escape(token, quote=True)}"
    toggle_action = f"/agent/toggle?token={escape(token, quote=True)}"
    enabled_checked = " checked" if spec.enabled else ""
    reusable_checked = " checked" if spec.reusable else ""
    run_hint = (
        "Agent mission accepted. Keep this tab open."
        if spec.enabled
        else "Resume this agent before running it."
    )
    clarification_session = clarification_session or ClarificationSession(
        context_key=_agent_context_key(spec.agent_id),
        default_target=f"agent:{spec.agent_id}",
    )
    clarify_html = _clarification_composer(
        store,
        token,
        session=clarification_session,
        context="agent",
        draft_name="request",
        run_action=action,
        run_label="Run Agent",
        running_label="Agent Running",
        draft_label="Mission for this agent",
        draft_placeholder="Run this agent on a specific task",
        submit_hint=run_hint,
        agent_id=spec.agent_id,
        disabled=not spec.enabled,
    )
    content = f"""
      {rows}
      {_inline_response(response or AppUiResponse())}
      <form method="post" action="{toggle_action}">
        <input type="hidden" name="agent_id" value="{escape(spec.agent_id, quote=True)}">
        <div class="actions">
          <button type="submit">{"Pause Agent" if spec.enabled else "Resume Agent"}</button>
        </div>
      </form>
      {clarify_html}
      <form method="post" action="{update_action}">
        <input type="hidden" name="agent_id" value="{escape(spec.agent_id, quote=True)}">
        <h2>Alter Agent</h2>
        <label>What this agent should do
          <textarea name="purpose" required>{escape(spec.purpose)}</textarea>
        </label>
        <label>What it can do well
          <textarea name="capabilities">{escape(_textarea_lines(spec.capabilities))}</textarea>
        </label>
        <label>Rules for this agent
          <textarea name="constraints">{escape(_textarea_lines(spec.constraints))}</textarea>
        </label>
        <label>When it should ask you
          <textarea name="interaction_points">{escape(_textarea_lines(spec.interaction_points))}</textarea>
        </label>
        <label class="check-row">
          <input type="checkbox" name="enabled"{enabled_checked}>
          <span>Agent is active</span>
        </label>
        <label class="check-row">
          <input type="checkbox" name="reusable"{reusable_checked}>
          <span>Reuse this agent for matching future missions</span>
        </label>
        <div class="actions">
          <button type="submit">Save Agent</button>
        </div>
      </form>
"""
    return _utility_page(
        "Run Agent",
        token,
        f"Run `{spec.agent_id}` directly while keeping the normal safety and memory flow.",
        content,
    )


def _inline_response(response: AppUiResponse) -> str:
    if response.busy:
        return f"""
      <div class="notice busy">
        <strong>Mission Running</strong>
        <p>{escape(response.message or "A mission is already running.")}</p>
      </div>
"""
    if response.error:
        return f"""
      <div class="notice error">
        <strong>Run Error</strong>
        <p>{escape(response.error)}</p>
      </div>
"""
    if response.message:
        return f"""
      <div class="notice">
        <strong>System Message</strong>
        <p>{escape(response.message)}</p>
      </div>
"""
    if response.result is None:
        return ""
    return f"""
      <div class="notice">
        <strong>Mission Result</strong>
        <p>Run <code>{escape(response.result.run_id)}</code></p>
        <div class="result">{escape(response.result.response)}</div>
      </div>
"""


def _submit_feedback_script() -> str:
    return """
  <script>
    (function () {
      const draftInput = document.querySelector('[data-clarify-draft]');
      const draftTargets = document.querySelectorAll('[data-draft-sync]');
      if (draftInput && draftTargets.length) {
        const syncDraft = () => {
          draftTargets.forEach((target) => { target.value = draftInput.value; });
        };
        draftInput.addEventListener('input', syncDraft);
        syncDraft();
      }
      function ensureStatus(form, button) {
        const existing = form.querySelector('.submit-status');
        if (existing) return existing;
        const status = document.createElement('span');
        status.className = 'submit-status';
        status.setAttribute('aria-live', 'polite');
        if (button && button.parentNode) {
          button.parentNode.insertBefore(status, button.nextSibling);
        } else {
          form.appendChild(status);
        }
        return status;
      }
      document.querySelectorAll('form[method="post"]').forEach((form) => {
        form.addEventListener('submit', (event) => {
          if (draftInput && draftTargets.length) {
            draftTargets.forEach((target) => { target.value = draftInput.value; });
          }
          if (form.dataset.submitted === 'true') {
            event.preventDefault();
            return;
          }
          form.dataset.submitted = 'true';
          const button = event.submitter || form.querySelector('button[type="submit"], input[type="submit"]');
          if (button) {
            button.disabled = true;
            const runningLabel = button.dataset.runningLabel || form.dataset.runningLabel || 'Working';
            if (button.tagName === 'INPUT') {
              button.value = runningLabel;
            } else {
              button.textContent = runningLabel;
            }
          }
          const status = ensureStatus(form, button);
          status.textContent = form.dataset.submitStatus || (button && button.dataset.submitStatus) || status.textContent || 'Working. Keep this tab open.';
          status.hidden = false;
        });
      });
      const popupInput = document.querySelector('[data-clarification-popup] input[name="answer"]');
      if (popupInput) popupInput.focus();

      const intakeForm = document.querySelector('[data-intake-form]');
      if (intakeForm) {
        const progress = intakeForm.querySelector('[data-intake-progress]');
        const startBtn = intakeForm.querySelector('[data-intake-start]');
        const defaultsBtn = intakeForm.querySelector('[data-intake-defaults]');
        const questions = Array.from(intakeForm.querySelectorAll('[data-intake-question]'));
        const isAnswered = (q) => {
          const radios = q.querySelectorAll('input[type="radio"]');
          if (radios.length) {
            const checked = q.querySelector('input[type="radio"]:checked');
            if (!checked) return false;
            if (checked.hasAttribute('data-intake-other')) {
              const other = q.querySelector('.intake-other-input');
              return !!other && other.value.trim().length > 0;
            }
            return true;
          }
          const text = q.querySelector('input[type="text"]');
          return text ? text.value.trim().length > 0 : true;
        };
        const refresh = () => {
          let answered = 0;
          questions.forEach((q) => {
            const otherRadio = q.querySelector('input[data-intake-other]');
            const otherInput = q.querySelector('.intake-other-input');
            if (otherRadio && otherInput) otherInput.hidden = !otherRadio.checked;
            if (isAnswered(q)) answered += 1;
          });
          if (progress) progress.textContent = answered + ' of ' + questions.length + ' answered';
          if (startBtn) startBtn.disabled = answered < questions.length;
        };
        intakeForm.addEventListener('change', (event) => {
          refresh();
          if (event.target && event.target.hasAttribute('data-intake-other')) {
            const other = event.target.closest('[data-intake-question]').querySelector('.intake-other-input');
            if (other) other.focus();
          }
        });
        intakeForm.addEventListener('input', refresh);
        if (defaultsBtn) {
          defaultsBtn.addEventListener('click', () => {
            questions.forEach((q) => {
              const rec = q.querySelector('.chip-rec');
              if (rec) {
                const radio = rec.closest('label').querySelector('input[type="radio"]');
                if (radio) radio.checked = true;
              }
              const otherInput = q.querySelector('.intake-other-input');
              if (otherInput) otherInput.hidden = true;
            });
            refresh();
          });
        }
        intakeForm.addEventListener('keydown', (event) => {
          if (event.key === 'Escape') {
            event.preventDefault();
            const edit = intakeForm.querySelector('.intake-edit');
            if (edit) edit.click();
          }
        });
        const firstControl = intakeForm.querySelector('input[type="radio"], .intake-text:not([hidden])');
        if (firstControl) firstControl.focus();
        refresh();
      }
    })();
  </script>
"""


def _diagnostics_page(paths: MatrixPaths, store: RuntimeStore, token: str) -> str:
    provider_config = store.get_default_provider_config()
    keymaker = Keymaker()
    onboarding_complete = store.get_preference("onboarding_complete") is True
    provider_label = "not configured"
    verification_label = "not checked"
    if provider_config is not None:
        provider_label = f"{provider_config.provider_id} / {provider_config.selected_model}"
        if provider_config.reasoning_effort:
            provider_label += f" / {provider_config.reasoning_effort}"
        verification = store.get_provider_verification(provider_config.provider_id)
        if verification is not None:
            verification_label = (
                "ok" if verification.get("ok") else f"failed: {verification.get('message')}"
            )
    rows = [
        ("Home folder", _status_text(paths.home.exists()), str(paths.home)),
        ("Vault folder", _status_text(paths.vault.exists()), str(paths.vault)),
        ("Runtime database", _status_text(paths.runtime_db.exists()), str(paths.runtime_db)),
        ("Prompt library", _status_text((paths.prompts_dir / "oracle_assess.md").exists()), ""),
        ("Secrets backend", keymaker.backend_name, f"writable={keymaker.can_write}"),
        ("Onboarding", _status_text(onboarding_complete), ""),
        ("Provider", provider_label, ""),
        ("Verification", verification_label, ""),
    ]
    return _utility_page(
        "System Check",
        token,
        "Local health checks for the runtime, memory, model access, and secret storage.",
        _rows_html(rows),
    )


def _memory_page(paths: MatrixPaths, store: RuntimeStore, token: str) -> str:
    counts = store.overview_counts()
    rows = [
        ("Vault", "human-readable memory", str(paths.vault)),
        ("Log", "timeline", str(paths.vault / "log.md")),
        ("Agents", f"{counts['agents']} reusable records", str(paths.vault / "wiki" / "agents")),
        ("Workflows", "mission ledgers", str(paths.vault / "wiki" / "workflows")),
        ("Raw runs", f"{counts['runs']} recorded missions", str(paths.vault / "raw" / "runs")),
        ("Prompt cache", f"{counts['prompt_blocks']} indexed blocks", str(paths.prompts_dir)),
        ("Runtime index", "SQLite metadata only", str(paths.runtime_db)),
    ]
    return _utility_page(
        "Memory",
        token,
        "The Matrix stores readable memory in the Obsidian vault and fast lookup metadata in SQLite.",
        _rows_html(rows),
    )


def _operator_page(
    store: RuntimeStore,
    token: str,
    goal_id: str = "",
    response: AppUiResponse | None = None,
) -> str:
    response = response or AppUiResponse()
    if goal_id:
        return _operator_goal_page(store, token, goal_id, response)
    goals = store.list_operator_goals(limit=20)
    runs = store.list_operator_goal_runs(limit=8)
    goal_items = []
    for goal in goals:
        schedule = (
            f"every {goal.schedule.interval_minutes} minute(s)" if goal.schedule else "no schedule"
        )
        next_run = goal.next_run_at.isoformat(timespec="seconds") if goal.next_run_at else "none"
        last_run = goal.last_run_at.isoformat(timespec="seconds") if goal.last_run_at else "none"
        message = str(goal.payload.get("message", ""))
        if goal.status == OperatorGoalStatus.PENDING:
            message = message or "Waiting for your activation before anything recurring starts."
        goal_items.append(
            f"""
        <div class="notice">
          <strong>{escape(goal.title)}</strong>
          <p class="muted">id=<code>{escape(goal.goal_id)}</code></p>
          <p class="muted">status={escape(goal.status.value)} schedule={escape(schedule)}</p>
          <p class="muted">next={escape(next_run)} last={escape(last_run)} failures={goal.failure_count}</p>
          <p class="result">{escape(message)}</p>
          <p><a class="button-link" href="{_operator_goal_url(token, goal.goal_id)}">Inspect Goal</a></p>
          <div class="operator-actions">
            {_operator_goal_actions(goal.goal_id, goal.status, token)}
          </div>
        </div>
"""
        )
    run_items = [
        f"""
        <div class="timeline-item">
          <p class="timeline-title">{escape(run.status.value)} / {escape(run.goal_id)}</p>
          <p class="muted">{escape(run.created_at.isoformat(timespec="seconds"))}</p>
          <p>{escape(run.message)}</p>
        </div>
"""
        for run in runs
    ]
    content = f"""
      {_inline_response(response)}
      <h2>Goals</h2>
      {''.join(goal_items) or '<p class="muted">No Operator goals are active yet.</p>'}
      <h2>Recent Operator Runs</h2>
      <div class="timeline">{''.join(run_items) or '<p class="muted">No Operator runs recorded yet.</p>'}</div>
"""
    return _utility_page(
        "The Operator",
        token,
        "Recurring goals live here while The Matrix app is running.",
        content,
    )


def _operator_goal_page(
    store: RuntimeStore,
    token: str,
    goal_id: str,
    response: AppUiResponse,
) -> str:
    goal = store.get_operator_goal(goal_id)
    if goal is None:
        return _utility_page(
            "Operator Goal Missing",
            token,
            "No Operator goal exists with that id.",
            _inline_response(AppUiResponse(error=f"No Operator goal exists with id `{goal_id}`.")),
            primary_action_label="The Operator",
            primary_action_url=_token_url("/operator", token),
        )
    runs = store.list_operator_goal_runs(goal_id=goal.goal_id, limit=12)
    schedule = f"Every {goal.schedule.interval_minutes} minute(s)" if goal.schedule else "Not scheduled"
    next_run = goal.next_run_at.isoformat(timespec="seconds") if goal.next_run_at else "Not scheduled"
    last_run = goal.last_run_at.isoformat(timespec="seconds") if goal.last_run_at else "No run yet"
    capability = goal.capability or "none"
    payload_message = str(goal.payload.get("message", "")).strip()
    explanation = _operator_goal_explanation(goal.status, goal.kind.value, capability)
    run_items = [
        f"""
        <div class="timeline-item">
          <p class="timeline-title">{escape(run.status.value)}</p>
          <p class="muted">{escape(run.created_at.isoformat(timespec="seconds"))}</p>
          <p>{escape(run.message)}</p>
        </div>
"""
        for run in runs
    ]
    rows = _rows_html(
        [
            ("Status", goal.status.value, explanation),
            ("Schedule", schedule, f"Next run: {next_run}"),
            ("Capability", capability, _operator_capability_note(capability)),
            ("Message", payload_message or "none", "This is the notification/body payload."),
            ("Last run", last_run, goal.last_result or "No result recorded yet."),
            ("Original request", goal.original_request, ""),
        ]
    )
    edit_form = _operator_goal_edit_form(goal, token)
    content = f"""
      {_inline_response(response)}
      {rows}
      {edit_form}
      <div class="notice">
        <strong>What The Operator Will Do</strong>
        <p>{escape(_operator_goal_plain_english(goal))}</p>
      </div>
      <div class="notice">
        <strong>What The Operator Will Not Do</strong>
        <p>{escape(_operator_goal_limits(goal))}</p>
      </div>
      <div class="operator-actions">
        {_operator_goal_actions(goal.goal_id, goal.status, token)}
      </div>
      <h2>Goal History</h2>
      <div class="timeline">{''.join(run_items) or '<p class="muted">No runs recorded for this goal yet.</p>'}</div>
"""
    return _utility_page(
        "Operator Goal",
        token,
        f"Inspect `{goal.title}` before letting it continue.",
        content,
        primary_action_label="The Operator",
        primary_action_url=_token_url("/operator", token),
    )


def _operator_goal_edit_form(goal, token: str) -> str:
    if goal.kind != OperatorGoalKind.RECURRING_NOTIFICATION:
        return ""
    interval = goal.schedule.interval_minutes if goal.schedule else 5
    message = str(goal.payload.get("message", ""))
    return f"""
      <div class="notice">
        <strong>Alter Recurring Goal</strong>
        <form method="post" action="/operator/update?token={escape(token, quote=True)}">
          <input type="hidden" name="goal_id" value="{escape(goal.goal_id, quote=True)}">
          <label>Goal name
            <input name="title" value="{escape(goal.title, quote=True)}" maxlength="80" required>
          </label>
          <label>Notification message
            <textarea name="message" required>{escape(message)}</textarea>
          </label>
          <label>Repeat every minutes
            <input name="interval_minutes" type="number" min="1" max="1440" value="{interval}" required>
          </label>
          <div class="actions">
            <button type="submit">Save Changes</button>
          </div>
        </form>
      </div>
"""


def _operator_goal_explanation(status: OperatorGoalStatus, kind: str, capability: str) -> str:
    if status == OperatorGoalStatus.PENDING:
        return "Nothing recurring will happen until you activate this goal."
    if status == OperatorGoalStatus.ACTIVE:
        return "The Operator may run this goal while the app is open."
    if status == OperatorGoalStatus.PAUSED:
        return "This goal is saved but will not run until resumed."
    if status == OperatorGoalStatus.CANCELED:
        return "This goal is stopped and will not run again."
    if status == OperatorGoalStatus.FAILED:
        return "This goal needs attention before it is trusted."
    if kind == "one_shot" or capability == "mission_run":
        return "This was tracked as a one-time delegated mission."
    return "This goal is recorded in The Operator."


def _operator_capability_note(capability: str) -> str:
    if capability == "notify_desktop":
        return "Allows a local desktop notification only."
    if capability == "mission_run":
        return "Tracks a one-time mission through the normal runtime."
    return "No external action capability is recorded."


def _operator_goal_plain_english(goal) -> str:
    if goal.kind.value == "recurring_notification" and goal.schedule is not None:
        message = str(goal.payload.get("message", "the reminder")).strip() or "the reminder"
        return (
            f"After activation, it will send a local desktop notification saying "
            f"`{message}` every {goal.schedule.interval_minutes} minute(s) while The Matrix app is running."
        )
    return "It tracks this delegated mission and records whether it completed or failed."


def _operator_goal_limits(goal) -> str:
    if goal.capability == "notify_desktop":
        return (
            "It will not read files, run shell commands, control apps, or keep running after "
            "The Matrix app is stopped."
        )
    return "It does not add extra permissions beyond the normal mission safety flow."


def _utility_page(
    title: str,
    token: str,
    intro: str,
    content: str,
    extra_script: str = "",
    primary_action_label: str = "New Mission",
    primary_action_url: str | None = None,
) -> str:
    dashboard_url = escape(_token_url("/dashboard", token), quote=True)
    ask_url = escape(primary_action_url or _token_url("/", token), quote=True)
    settings_url = escape(_token_url("/settings", token), quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>The Matrix {escape(title)}</title>
  <style>
    body {{ margin: 0; background: #000; color: #00b341; font: 15px/1.6 "Cascadia Mono", "Courier New", monospace; }}
    {matrix_background_styles("0.36")}
    main {{ position: relative; z-index: 2; width: min(920px, calc(100% - 32px)); margin: 0 auto; padding: 42px 0; }}
    section {{ background: rgba(0,14,4,0.84); border: 1px solid rgba(0,255,65,0.14); border-left: 2px solid #00ff41; padding: 22px; }}
    h1 {{ margin: 0 0 8px; color: #00ff41; font-size: 38px; }}
    p {{ margin: 0 0 18px; color: #7cff9d; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 0 0 22px; }}
    a {{ color: #00ff41; }}
    .button-link {{ border: 1px solid #00ff41; padding: 10px 13px; text-decoration: none; text-transform: uppercase; font-size: 13px; }}
    .button-link:hover {{ background: rgba(0,255,65,0.1); }}
    form {{ margin-top: 22px; }}
    label {{ display: grid; gap: 10px; color: #7cff9d; text-transform: uppercase; letter-spacing: 1.4px; font-size: 12px; }}
    .check-row {{ display: inline-flex; width: fit-content; max-width: 100%; grid-template-columns: none; align-items: center; gap: 10px; margin-top: 14px; }}
    .check-row input[type="checkbox"] {{ flex: 0 0 auto; width: 16px; height: 16px; margin: 0; accent-color: #00ff41; }}
    .check-row span {{ overflow-wrap: anywhere; }}
    textarea {{ width: 100%; min-height: 140px; resize: vertical; border: 1px solid rgba(0,255,65,0.18); background: rgba(0,8,2,0.86); color: #00ff41; font: inherit; padding: 12px; outline: none; }}
    input, select {{ width: 100%; border: 1px solid rgba(0,255,65,0.18); background: rgba(0,8,2,0.86); color: #00ff41; font: inherit; padding: 10px; outline: none; }}
    input[type="hidden"] {{ display: none; }}
    textarea:focus, input:focus, select:focus {{ border-color: #00ff41; box-shadow: 0 0 0 1px #00ff41; }}
    button {{ border: 1px solid #00ff41; background: transparent; color: #00ff41; cursor: pointer; font: inherit; padding: 10px 13px; text-transform: uppercase; }}
    button:hover {{ background: rgba(0,255,65,0.1); }}
    button:disabled {{ cursor: wait; opacity: 0.68; border-color: #7cff9d; color: #7cff9d; }}
    .submit-status {{ color: #7cff9d; font-size: 12px; letter-spacing: 1.4px; text-transform: uppercase; }}
    .submit-status::after {{ content: ' _'; animation: statusBlink 1.05s step-end infinite; }}
    .notice {{ border-top: 1px dashed rgba(0,255,65,0.16); margin-top: 18px; padding-top: 14px; }}
    .notice.error {{ color: #ff003c; }}
    .notice.busy {{ color: #7cff9d; }}
    .clarification-popup {{ position: fixed; inset: 0; z-index: 1200; display: grid; place-items: center; padding: 24px; background: rgba(0,0,0,0.74); }}
    .clarification-dialog {{ width: min(720px, 100%); max-height: min(720px, calc(100vh - 48px)); overflow: auto; border: 1px solid #00ff41; border-left: 2px solid #00ff41; background: rgba(0,14,4,0.96); box-shadow: 0 0 0 1px rgba(0,255,65,0.18), 0 0 34px rgba(0,255,65,0.22); padding: 22px; }}
    .clarification-dialog h3 {{ margin: 0 0 12px; color: #00ff41; font-size: 16px; font-weight: normal; letter-spacing: 2px; text-transform: uppercase; }}
    .clarification-question {{ color: #00ff41; font-size: 16px; margin-bottom: 18px; white-space: pre-wrap; }}
    .clarification-inline-note {{ border: 1px solid rgba(0,255,65,0.26); padding: 12px; }}
    .oracle-stream {{ border: 1px solid rgba(0,255,65,0.32); border-left: 2px solid #00ff41; margin: 0 0 22px; padding: 16px; box-shadow: 0 0 22px rgba(0,255,65,0.12); }}
    .oracle-scan {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin: 10px 0 14px; }}
    .oracle-scan span {{ height: 2px; background: linear-gradient(90deg, transparent, #00ff41, transparent); animation: scanPulse 1.2s ease-in-out infinite; }}
    .oracle-scan span:nth-child(2) {{ animation-delay: 180ms; }}
    .oracle-scan span:nth-child(3) {{ animation-delay: 360ms; }}
    .oracle-output {{ min-height: 96px; overflow-wrap: anywhere; }}
    @keyframes statusBlink {{ 0%, 49% {{ opacity: 1; }} 50%, 100% {{ opacity: 0; }} }}
    @keyframes scanPulse {{ 0%, 100% {{ opacity: 0.25; transform: scaleX(0.55); }} 50% {{ opacity: 1; transform: scaleX(1); }} }}
    .result-actions {{ margin-top: 14px; }}
    .operator-actions {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }}
    .operator-actions form {{ margin: 0; }}
    .operator-actions button {{ font-size: 13px; letter-spacing: 1.4px; margin-top: 0; padding: 8px 10px; }}
    .clarify-grid {{ display: grid; gap: 14px; }}
    .clarify-row {{ display: grid; grid-template-columns: minmax(180px, 260px) 1fr; gap: 14px; align-items: end; }}
    .transcript {{ display: grid; gap: 10px; }}
    .turn {{ border-top: 1px dashed rgba(0,255,65,0.16); padding-top: 10px; }}
    .turn:first-child {{ border-top: 0; padding-top: 0; }}
    .turn-label {{ color: #7cff9d; font-size: 12px; letter-spacing: 1.3px; text-transform: uppercase; }}
    .mission-summary {{ border-bottom: 1px dashed rgba(0,255,65,0.16); margin-bottom: 18px; padding-bottom: 16px; }}
    .mission-summary h2 {{ border: 0; margin: 0 0 8px; padding: 0; color: #00ff41; font-size: 24px; }}
    .approval-panel {{ border: 1px solid rgba(0,255,65,0.28); margin: 0 0 18px; padding: 16px; box-shadow: 0 0 18px rgba(0,255,65,0.12); }}
    .approval-card {{ border-top: 1px dashed rgba(0,255,65,0.16); padding-top: 12px; }}
    .approval-card:first-child {{ border-top: 0; padding-top: 0; }}
    .approval-actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 12px; }}
    .approval-actions button {{ margin-top: 0; }}
    .kicker {{ color: #7cff9d; font-size: 12px; letter-spacing: 1.4px; text-transform: uppercase; }}
    .request-text {{ color: #00ff41; overflow-wrap: anywhere; }}
    .mission-contract {{ border-bottom: 1px dashed rgba(0,255,65,0.16); margin-bottom: 18px; padding-bottom: 18px; }}
    .mission-contract h2, .mission-contract h3 {{ color: #00ff41; margin: 0 0 10px; }}
    .mission-contract h2 {{ font-size: 24px; }}
    .mission-contract h3 {{ font-size: 17px; margin-top: 18px; text-transform: uppercase; letter-spacing: 1px; }}
    .contract-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px 18px; margin-bottom: 8px; }}
    .contract-field {{ border-top: 1px dashed rgba(0,255,65,0.16); padding-top: 10px; min-width: 0; }}
    .contract-field p:last-child {{ color: #00ff41; overflow-wrap: anywhere; }}
    .execution-path {{ display: grid; gap: 12px; }}
    .execution-step {{ border-top: 1px dashed rgba(0,255,65,0.16); padding-top: 12px; }}
    .execution-step-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }}
    .execution-step-head .timeline-title {{ margin-bottom: 8px; }}
    .execution-details {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 16px; margin-top: 10px; }}
    .execution-details .contract-field {{ padding-top: 8px; }}
    .mission-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-top: 18px; }}
    .timeline {{ display: grid; gap: 10px; }}
    .timeline-item {{ border-top: 1px dashed rgba(0,255,65,0.16); padding-top: 10px; }}
    .timeline-title {{ color: #00ff41; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 1px; }}
    .pill {{ display: inline-flex; border: 1px solid rgba(0,255,65,0.22); color: #7cff9d; font-size: 11px; letter-spacing: 1.2px; padding: 2px 8px; text-transform: uppercase; }}
    .result {{ white-space: pre-wrap; color: #00ff41; }}
    .row {{ display: grid; grid-template-columns: 180px 1fr; gap: 12px; padding: 11px 0; border-top: 1px dashed rgba(0,255,65,0.16); }}
    .row:first-child {{ border-top: 0; }}
    .key {{ color: #7cff9d; text-transform: uppercase; letter-spacing: 1.4px; font-size: 12px; }}
    .value {{ color: #00ff41; overflow-wrap: anywhere; }}
    .note {{ color: #00b341; overflow-wrap: anywhere; }}
    @media (max-width: 720px) {{ .row, .mission-grid, .contract-grid, .execution-details, .clarify-row {{ grid-template-columns: 1fr; }} .execution-step-head {{ display: block; }} }}
  </style>
</head>
<body>
  {matrix_background_canvas()}
  <main>
    <section>
      <h1>{escape(title)}</h1>
      <p>{escape(intro)}</p>
      <div class="actions">
        <a class="button-link" href="{dashboard_url}">Back to Dashboard</a>
        <a id="primary-action-link" class="button-link" href="{ask_url}">{escape(primary_action_label)}</a>
        <a class="button-link" href="{settings_url}">Change Model</a>
      </div>
      {content}
    </section>
  </main>
  {matrix_rain_script()}
  {_submit_feedback_script()}
  {extra_script}
</body>
</html>
"""


def _token_url(path: str, token: str, **params: str) -> str:
    query = {"token": token}
    query.update({key: value for key, value in params.items() if value})
    return f"{path}?{urlencode(query)}"


def _rows_html(rows: list[tuple[str, str, str]]) -> str:
    items = "\n".join(
        f"""
        <div class="row">
          <div class="key">{escape(key)}</div>
          <div>
            <div class="value">{escape(value)}</div>
            <div class="note">{escape(note)}</div>
          </div>
        </div>
"""
        for key, value, note in rows
    )
    return f"<div>{items}</div>"


def _status_text(ok: bool) -> str:
    return "ok" if ok else "needs attention"


def _message_page(title: str, message: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>The Matrix {escape(title)}</title>
  <style>
    body {{ margin: 0; background: #000; color: #00b341; font: 15px/1.55 "Cascadia Mono", "Courier New", monospace; }}
    {matrix_background_styles("0.34")}
    main {{ position: relative; z-index: 2; width: min(720px, calc(100% - 32px)); margin: 0 auto; padding: 42px 0; }}
    section {{ background: rgba(0,14,4,0.82); border: 1px solid rgba(0,255,65,0.14); border-left: 2px solid #00ff41; padding: 20px; }}
    h1 {{ margin: 0 0 8px; color: #00ff41; }}
    p {{ margin: 0; color: #7cff9d; }}
  </style>
</head>
<body>
  {matrix_background_canvas()}
  <main><section><h1>{escape(title)}</h1><p>{escape(message)}</p></section></main>
  {matrix_rain_script()}
</body>
</html>
"""


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."
