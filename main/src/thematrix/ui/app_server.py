from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
from inspect import Parameter, signature
import json
from secrets import token_urlsafe
from threading import Lock, Thread, Timer
from urllib.parse import parse_qs, urlencode, urlparse
import webbrowser

from thematrix.config import MatrixPaths
from thematrix.memory import MemoryVault, RuntimeStore
from thematrix.prompts import PromptLibrary
from thematrix.providers import detect_local_providers
from thematrix.providers.oauth import (
    OAuthPendingSetup,
    OAuthProviderError,
    build_openrouter_oauth_setup,
    exchange_openrouter_code,
    setup_form_from_oauth,
)
from thematrix.schemas import AgentSpec, MatrixRunResult
from thematrix.security import Keymaker
from thematrix.ui.dashboard import render_dashboard_html, write_dashboard
from thematrix.ui.setup_server import apply_setup_form, render_setup_form

MAX_APP_BODY_BYTES = 64 * 1024
DEFAULT_APP_TIMEOUT_SECONDS = 60 * 60


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


def serve_app_ui(
    paths: MatrixPaths,
    vault: MemoryVault,
    store: RuntimeStore,
    request_runner: Callable[..., MatrixRunResult],
    agent_request_runner: Callable[..., MatrixRunResult] | None = None,
    port: int = 0,
    open_browser: bool = True,
    url_callback: Callable[[str], None] | None = None,
    timeout_seconds: int = DEFAULT_APP_TIMEOUT_SECONDS,
) -> str:
    token = token_urlsafe(24)
    run_lock = Lock()
    mission_registry = MissionRegistry()
    server = _AppServer(
        ("127.0.0.1", port),
        _handler_factory(
            paths,
            vault,
            store,
            token,
            request_runner,
            agent_request_runner,
            run_lock,
            mission_registry,
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
    run_lock: Lock,
    mission_registry: MissionRegistry,
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
                self._send_html(HTTPStatus.OK, render_app_page(paths, store, token))
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
                self._send_html(HTTPStatus.OK, render_dashboard_html(paths, store, token))
                return
            if parsed.path == "/mission":
                query = parse_qs(parsed.query)
                job_id = query.get("job_id", [""])[-1].strip()
                run_id = query.get("run_id", [""])[-1].strip()
                self._send_html(
                    HTTPStatus.OK,
                    _mission_page(store, token, mission_registry.get(job_id), run_id=run_id),
                )
                return
            if parsed.path == "/mission/status":
                query = parse_qs(parsed.query)
                job_id = query.get("job_id", [""])[-1].strip()
                run_id = query.get("run_id", [""])[-1].strip()
                payload = _mission_payload(store, mission_registry.get(job_id), run_id=run_id)
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
                self._send_html(status, _agent_page(paths, store, token, agent_id))
                return
            if parsed.path == "/diagnostics":
                self._send_html(HTTPStatus.OK, _diagnostics_page(paths, store, token))
                return
            if parsed.path == "/memory":
                self._send_html(HTTPStatus.OK, _memory_page(paths, store, token))
                return
            self._send_html(HTTPStatus.NOT_FOUND, _message_page("Not Found", "Unknown route."))

        def do_POST(self) -> None:
            if not self._token_ok():
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
                job = mission_registry.create("mission", user_request)
                Thread(
                    target=_run_background_mission,
                    args=(job, request_runner, user_request, run_lock),
                    daemon=True,
                ).start()
                self._send_html(HTTPStatus.ACCEPTED, _mission_page(store, token, job))
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
                        ),
                    )
                    return
                message = "Agent resumed." if spec.enabled else "Agent paused."
                self._send_html(
                    HTTPStatus.OK,
                    _agent_page(paths, store, token, spec.agent_id, AppUiResponse(message=message)),
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
                        ),
                    )
                    return
                job = mission_registry.create("agent", user_request, agent_id=agent_id)
                Thread(
                    target=_run_background_agent_mission,
                    args=(job, agent_request_runner, agent_id, user_request, run_lock),
                    daemon=True,
                ).start()
                self._send_html(HTTPStatus.ACCEPTED, _mission_page(store, token, job))
                return
            self._send_html(HTTPStatus.NOT_FOUND, _message_page("Not Found", "Unknown route."))

        def _start_openrouter_oauth(self, parsed) -> None:
            form = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            host, port = self.server.server_address[:2]

            def callback_url_for_flow(flow_id: str) -> str:
                return (
                    f"http://{host}:{port}/oauth/openrouter/callback?"
                    + urlencode({"flow": flow_id})
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
            code = query.get("code", [""])[-1]
            if not flow_id or not code:
                self._send_html(
                    HTTPStatus.BAD_REQUEST,
                    _message_page("OAuth incomplete", "The provider did not return a usable code."),
                )
                return
            with oauth_lock:
                pending = oauth_flows.pop(flow_id, None)
            if pending is None:
                self._send_html(
                    HTTPStatus.BAD_REQUEST,
                    _message_page("OAuth expired", "Start provider sign-in again from settings."),
                )
                return
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

        def log_message(self, format: str, *args: object) -> None:
            return

        def _token_ok(self) -> bool:
            query = parse_qs(urlparse(self.path).query)
            supplied = query.get("token", [""])[-1]
            return hmac.compare_digest(supplied, token)

        def _send_redirect(self, location: str) -> None:
            self.send_response(HTTPStatus.FOUND.value)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()

        def _send_html(self, status: HTTPStatus, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return AppHandler


def _run_background_mission(
    job: MissionJob,
    request_runner: Callable[..., MatrixRunResult],
    request: str,
    run_lock: Lock,
) -> None:
    job.start()
    try:
        result = _call_runner(request_runner, request, job.record)
    except Exception as exc:
        job.fail(f"Mission failed: {exc}")
    else:
        job.complete(result)
    finally:
        run_lock.release()


def _run_background_agent_mission(
    job: MissionJob,
    agent_request_runner: Callable[..., MatrixRunResult],
    agent_id: str,
    request: str,
    run_lock: Lock,
) -> None:
    job.start()
    try:
        result = _call_agent_runner(agent_request_runner, agent_id, request, job.record)
    except Exception as exc:
        job.fail(f"Agent run failed: {exc}")
    else:
        job.complete(result)
    finally:
        run_lock.release()


def _call_runner(
    runner: Callable[..., MatrixRunResult],
    request: str,
    progress_callback: Callable[[str, str, dict[str, object]], None],
) -> MatrixRunResult:
    if _accepts_progress_callback(runner):
        return runner(request, progress_callback=progress_callback)
    return runner(request)


def _call_agent_runner(
    runner: Callable[..., MatrixRunResult],
    agent_id: str,
    request: str,
    progress_callback: Callable[[str, str, dict[str, object]], None],
) -> MatrixRunResult:
    if _accepts_progress_callback(runner):
        return runner(agent_id, request, progress_callback=progress_callback)
    return runner(agent_id, request)


def _accepts_progress_callback(runner: Callable[..., MatrixRunResult]) -> bool:
    try:
        parameters = signature(runner).parameters.values()
    except (TypeError, ValueError):
        return False
    for parameter in parameters:
        if parameter.kind == Parameter.VAR_KEYWORD:
            return True
        if parameter.name == "progress_callback":
            return True
    return False


def _mission_page(
    store: RuntimeStore,
    token: str,
    job: MissionJob | None,
    run_id: str = "",
) -> str:
    payload = _mission_payload(store, job, run_id=run_id)
    if not payload["found"]:
        return _utility_page(
            "Mission Not Found",
            token,
            "No mission record exists for that id.",
            "",
        )

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
        extra_script=_mission_status_script(status_url, payload),
    )


def _mission_payload(
    store: RuntimeStore,
    job: MissionJob | None,
    run_id: str = "",
) -> dict[str, object]:
    if job is not None:
        with job.lock:
            result = job.result
            resolved_run_id = result.run_id if result is not None else ""
            return {
                "found": True,
                "job_id": job.job_id,
                "kind": job.kind,
                "status": job.status,
                "stage": job.stage,
                "message": job.message,
                "request": job.request,
                "agent_id": job.agent_id,
                "created_at": job.created_at,
                "started_at": job.started_at,
                "completed_at": job.completed_at,
                "run_id": resolved_run_id,
                "events": [_event_payload(event) for event in job.events],
                "tasks": _task_payloads(store, resolved_run_id),
                "result": _result_payload(result),
                "error": job.error,
            }

    if run_id:
        result = store.get_run(run_id)
        if result is None:
            return {"found": False}
        return {
            "found": True,
            "job_id": "",
            "kind": "recorded",
            "status": _run_status(result),
            "stage": "recorded",
            "message": "This mission is recorded in local memory.",
            "request": result.request,
            "agent_id": result.agent_spec.agent_id if result.agent_spec else None,
            "created_at": result.created_at.isoformat(),
            "started_at": None,
            "completed_at": result.created_at.isoformat(),
            "run_id": result.run_id,
            "events": _events_from_result(result),
            "tasks": _task_payloads(store, result.run_id),
            "result": _result_payload(result),
            "error": result.metadata.get("agent_execution_error"),
        }
    return {"found": False}


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
    return [
        {
            "sequence": task.sequence,
            "title": task.title,
            "status": task.status.value,
            "agent_id": task.agent_spec.agent_id,
            "agent_type": task.agent_spec.agent_type,
            "result_summary": task.result_summary,
            "tool_result_count": task.tool_result_count,
            "error": task.error,
        }
        for task in store.list_mission_tasks(run_id=run_id, limit=100)
    ]


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


def _mission_status_script(status_url: str, payload: dict[str, object]) -> str:
    return f"""
  <script>
    (function () {{
      const statusUrl = {json.dumps(status_url)};
      let payload = {_script_json(payload)};
      const state = document.getElementById('mission-state');
      const message = document.getElementById('mission-message');
      const request = document.getElementById('mission-request');
      const events = document.getElementById('mission-events');
      const tasks = document.getElementById('mission-tasks');
      const result = document.getElementById('mission-result');

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

      function render(next) {{
        payload = next;
        state.textContent = String(payload.status || 'unknown').toUpperCase();
        message.textContent = payload.message || '';
        request.textContent = payload.request || '';
        clear(events);
        (payload.events || []).forEach((event) => {{
          events.appendChild(line(event.stage || 'event', event.message || '', event.created_at || ''));
        }});
        if (!(payload.events || []).length) {{
          events.appendChild(line('waiting', 'The mission has been accepted.', 'now'));
        }}
        clear(tasks);
        (payload.tasks || []).forEach((task) => {{
          tasks.appendChild(line(
            (task.sequence || '?') + '. ' + (task.title || 'Task'),
            'Agent: ' + (task.agent_id || 'unknown'),
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


def render_app_page(
    paths: MatrixPaths,
    store: RuntimeStore,
    token: str,
    response: AppUiResponse | None = None,
) -> str:
    response = response or AppUiResponse()
    provider_config = store.get_default_provider_config()
    provider_label = "unconfigured"
    if provider_config is not None:
        provider_label = f"{provider_config.provider_id} / {provider_config.selected_model}"
        if provider_config.reasoning_effort:
            provider_label += f" / {provider_config.reasoning_effort}"
    result_html = _result_panel(response)
    recent_html = _recent_runs_panel(store, token)
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
    @media (max-width: 760px) {{
      main {{ width: min(1040px, calc(100% - 28px)); }}
      h1 {{ font-size: 54px; letter-spacing: 3px; }}
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
    <section class="panel">
      <h2>Transmit Request</h2>
      <form method="post" action="/ask?token={escape(token)}" data-mission-form>
        <label>What do you want the agents to do?
          <textarea name="request" placeholder="Create a reusable research agent for comparing AI tools" required></textarea>
        </label>
        <div class="actions">
          <button type="submit" data-running-label="Mission Running">Run Mission</button>
          <a class="button-link" href="/dashboard?token={escape(token)}">Back to Dashboard</a>
          <a class="button-link" href="/settings?token={escape(token)}">Provider Settings</a>
          <span class="submit-status" hidden aria-live="polite">
            Mission accepted. Keep this tab open.
          </span>
        </div>
      </form>
    </section>
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
    (function () {{
      document.querySelectorAll('[data-mission-form]').forEach((form) => {{
        form.addEventListener('submit', (event) => {{
          if (form.dataset.submitted === 'true') {{
            event.preventDefault();
            return;
          }}
          form.dataset.submitted = 'true';
          const button = form.querySelector('button[type="submit"]');
          if (button) {{
            button.disabled = true;
            button.textContent = button.dataset.runningLabel || 'Running';
          }}
          const status = form.querySelector('.submit-status');
          if (status) status.hidden = false;
        }});
      }});
    }})();
  </script>
</body>
</html>
"""


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


def _recent_runs_panel(store: RuntimeStore, token: str) -> str:
    runs = store.list_run_records(limit=5)
    items = []
    for run in runs:
        href = f"/mission?token={escape(token, quote=True)}&run_id={escape(run['run_id'], quote=True)}"
        items.append(
            f"""
        <div class="item">
          <p><a href="{href}"><code>{escape(run["run_id"])}</code></a></p>
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


def _help_panel() -> str:
    return """
    <details class="panel">
      <summary>Help / Commands</summary>
      <div class="list help-list">
        <div class="item">
          <p><strong>Run from browser</strong></p>
          <p class="muted">Use the request box above for normal agent missions.</p>
        </div>
        <div class="item">
          <p><strong>Change provider</strong></p>
          <p class="muted">Open Provider Settings in this page, choose provider/model, then save and test.</p>
        </div>
        <div class="item">
          <p><strong>Useful terminal commands</strong></p>
          <p><code>the-matrix start</code></p>
          <p><code>the-matrix ask "your request"</code></p>
          <p><code>the-matrix providers current</code></p>
          <p><code>the-matrix doctor</code></p>
          <p><code>the-matrix ui --open</code></p>
          <p><code>the-matrix memory summary</code></p>
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
    run_disabled = " disabled" if not spec.enabled else ""
    run_hint = (
        "Agent mission accepted. Keep this tab open."
        if spec.enabled
        else "Resume this agent before running it."
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
      <form method="post" action="{action}" data-mission-form>
        <input type="hidden" name="agent_id" value="{escape(spec.agent_id, quote=True)}">
        <label>Mission for this agent
          <textarea name="request" placeholder="Run this agent on a specific task" required></textarea>
        </label>
        <div class="actions">
          <button type="submit" data-running-label="Agent Running"{run_disabled}>Run Agent</button>
          <span class="submit-status" hidden aria-live="polite">
            {escape(run_hint)}
          </span>
        </div>
      </form>
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
          Agent is active
        </label>
        <label class="check-row">
          <input type="checkbox" name="reusable"{reusable_checked}>
          Reuse this agent for matching future missions
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
        extra_script=_mission_submit_script(),
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


def _mission_submit_script() -> str:
    return """
  <script>
    (function () {
      document.querySelectorAll('[data-mission-form]').forEach((form) => {
        form.addEventListener('submit', (event) => {
          if (form.dataset.submitted === 'true') {
            event.preventDefault();
            return;
          }
          form.dataset.submitted = 'true';
          const button = form.querySelector('button[type="submit"]');
          if (button) {
            button.disabled = true;
            button.textContent = button.dataset.runningLabel || 'Running';
          }
          const status = form.querySelector('.submit-status');
          if (status) status.hidden = false;
        });
      });
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


def _utility_page(
    title: str,
    token: str,
    intro: str,
    content: str,
    extra_script: str = "",
) -> str:
    dashboard_url = f"/dashboard?token={escape(token, quote=True)}"
    ask_url = f"/?token={escape(token, quote=True)}"
    settings_url = f"/settings?token={escape(token, quote=True)}"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>The Matrix {escape(title)}</title>
  <style>
    body {{ margin: 0; background: #000; color: #00b341; font: 15px/1.6 "Cascadia Mono", "Courier New", monospace; }}
    main {{ width: min(920px, calc(100% - 32px)); margin: 0 auto; padding: 42px 0; }}
    section {{ background: rgba(0,14,4,0.84); border: 1px solid rgba(0,255,65,0.14); border-left: 2px solid #00ff41; padding: 22px; }}
    h1 {{ margin: 0 0 8px; color: #00ff41; font-size: 38px; }}
    p {{ margin: 0 0 18px; color: #7cff9d; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 0 0 22px; }}
    a {{ color: #00ff41; }}
    .button-link {{ border: 1px solid #00ff41; padding: 10px 13px; text-decoration: none; text-transform: uppercase; font-size: 13px; }}
    .button-link:hover {{ background: rgba(0,255,65,0.1); }}
    form {{ margin-top: 22px; }}
    label {{ display: grid; gap: 10px; color: #7cff9d; text-transform: uppercase; letter-spacing: 1.4px; font-size: 12px; }}
    .check-row {{ display: flex; grid-template-columns: none; align-items: center; gap: 10px; }}
    input[type="checkbox"] {{ accent-color: #00ff41; }}
    textarea {{ width: 100%; min-height: 140px; resize: vertical; border: 1px solid rgba(0,255,65,0.18); background: rgba(0,8,2,0.86); color: #00ff41; font: inherit; padding: 12px; outline: none; }}
    textarea:focus {{ border-color: #00ff41; box-shadow: 0 0 0 1px #00ff41; }}
    button {{ border: 1px solid #00ff41; background: transparent; color: #00ff41; cursor: pointer; font: inherit; padding: 10px 13px; text-transform: uppercase; }}
    button:hover {{ background: rgba(0,255,65,0.1); }}
    button:disabled {{ cursor: wait; opacity: 0.68; border-color: #7cff9d; color: #7cff9d; }}
    .submit-status {{ color: #7cff9d; font-size: 12px; letter-spacing: 1.4px; text-transform: uppercase; }}
    .notice {{ border-top: 1px dashed rgba(0,255,65,0.16); margin-top: 18px; padding-top: 14px; }}
    .notice.error {{ color: #ff003c; }}
    .notice.busy {{ color: #7cff9d; }}
    .mission-summary {{ border-bottom: 1px dashed rgba(0,255,65,0.16); margin-bottom: 18px; padding-bottom: 16px; }}
    .mission-summary h2 {{ border: 0; margin: 0 0 8px; padding: 0; color: #00ff41; font-size: 24px; }}
    .kicker {{ color: #7cff9d; font-size: 12px; letter-spacing: 1.4px; text-transform: uppercase; }}
    .request-text {{ color: #00ff41; overflow-wrap: anywhere; }}
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
    @media (max-width: 720px) {{ .row, .mission-grid {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <main>
    <section>
      <h1>{escape(title)}</h1>
      <p>{escape(intro)}</p>
      <div class="actions">
        <a class="button-link" href="{dashboard_url}">Back to Dashboard</a>
        <a class="button-link" href="{ask_url}">Ask Agent</a>
        <a class="button-link" href="{settings_url}">Change Model</a>
      </div>
      {content}
    </section>
  </main>
  {extra_script}
</body>
</html>
"""


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
    main {{ width: min(720px, calc(100% - 32px)); margin: 0 auto; padding: 42px 0; }}
    section {{ background: rgba(0,14,4,0.82); border: 1px solid rgba(0,255,65,0.14); border-left: 2px solid #00ff41; padding: 20px; }}
    h1 {{ margin: 0 0 8px; color: #00ff41; }}
    p {{ margin: 0; color: #7cff9d; }}
  </style>
</head>
<body><main><section><h1>{escape(title)}</h1><p>{escape(message)}</p></section></main></body>
</html>
"""


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."
