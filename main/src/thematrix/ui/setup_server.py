from __future__ import annotations

from dataclasses import dataclass
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from secrets import token_urlsafe
from threading import Thread, Timer
from typing import Callable
from urllib.parse import parse_qs, urlparse
import webbrowser

from thematrix.config import MatrixPaths
from thematrix.memory import MemoryVault, RuntimeStore
from thematrix.onboarding import OnboardingService
from thematrix.providers import (
    ProviderDetection,
    default_model_gateway,
    detect_local_providers,
    provider_catalog,
)
from thematrix.schemas import (
    AuthMode,
    FileChangeConsent,
    OnboardingProfile,
    PrivacyMode,
    ProviderConfig,
    ProviderProfile,
)
from thematrix.security import Keymaker, SecretStoreError

MAX_SETUP_BODY_BYTES = 64 * 1024
DEFAULT_SETUP_TIMEOUT_SECONDS = 15 * 60


@dataclass
class SetupUiResult:
    ok: bool
    message: str
    provider_id: str | None = None
    tested: bool = False


def serve_setup_ui(
    paths: MatrixPaths,
    vault: MemoryVault,
    store: RuntimeStore,
    port: int = 0,
    open_browser: bool = True,
    keymaker_factory: Callable[[], Keymaker] = Keymaker,
    url_callback: Callable[[str], None] | None = None,
    timeout_seconds: int = DEFAULT_SETUP_TIMEOUT_SECONDS,
) -> str:
    token = token_urlsafe(24)
    server = _SetupServer(
        ("127.0.0.1", port),
        _handler_factory(paths, vault, store, token, keymaker_factory),
    )
    host, bound_port = server.server_address
    url = f"http://{host}:{bound_port}/?token={token}"
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


def apply_setup_form(
    form: dict[str, str],
    paths: MatrixPaths,
    vault: MemoryVault,
    store: RuntimeStore,
    keymaker: Keymaker,
) -> SetupUiResult:
    profiles = {profile.provider_id: profile for profile in provider_catalog()}
    provider_id = form.get("provider_id", "").strip()
    profile = profiles.get(provider_id)
    if profile is None:
        return SetupUiResult(ok=False, message="Choose a valid provider.")

    try:
        auth_mode = AuthMode(form.get("auth_mode", ""))
    except ValueError:
        return SetupUiResult(ok=False, message="Choose a valid auth mode.")
    if auth_mode not in profile.auth_modes:
        return SetupUiResult(
            ok=False,
            message=f"{profile.display_name} does not support auth mode `{auth_mode.value}`.",
        )
    if auth_mode == AuthMode.OAUTH:
        return SetupUiResult(
            ok=False,
            message="OAuth setup is not wired yet. Use API key or local auth for this version.",
        )

    selected_model = form.get("model", "").strip()
    if not selected_model:
        selected_model = profile.suggested_models[0] if profile.suggested_models else "default"
    base_url = form.get("base_url", "").strip() or profile.default_base_url

    secret_ref = None
    api_key = form.get("api_key", "").strip()
    if auth_mode in {AuthMode.API_KEY, AuthMode.LOCAL_TOKEN}:
        if not api_key:
            return SetupUiResult(ok=False, message="Enter an API key or token for this provider.")
        try:
            secret_ref = keymaker.store_api_key(provider_id, api_key).secret_ref
        except SecretStoreError as exc:
            return SetupUiResult(ok=False, message=str(exc))

    privacy_mode = PrivacyMode(form.get("privacy_mode", PrivacyMode.ASK_EACH_TIME.value))
    file_consent = FileChangeConsent(
        form.get("file_change_consent", FileChangeConsent.ASK_EACH_TIME.value)
    )
    guarded_shell_enabled = form.get("guarded_shell_enabled") == "on"

    provider_config = ProviderConfig(
        provider_id=provider_id,
        selected_model=selected_model,
        auth_mode=auth_mode,
        secret_ref=secret_ref,
        base_url=base_url,
        is_default=True,
        file_change_consent=file_consent,
    )
    onboarding_profile = OnboardingProfile(
        default_provider_id=provider_id,
        default_model=selected_model,
        auth_mode=auth_mode,
        base_url=base_url,
        privacy_mode=privacy_mode,
        file_change_consent=file_consent,
        guarded_shell_enabled=guarded_shell_enabled,
        vault_path=str(paths.vault),
        secret_configured=secret_ref is not None,
    )
    OnboardingService(store, vault).complete(onboarding_profile, provider_config)

    tested = form.get("test_provider") == "on"
    if tested:
        health = default_model_gateway(store).health_check(provider_config)
        store.record_provider_verification(
            provider_id=provider_id,
            ok=health.ok,
            message=health.message,
            model=selected_model,
        )
        if not health.ok:
            return SetupUiResult(
                ok=True,
                provider_id=provider_id,
                tested=True,
                message=f"Saved setup, but provider test failed: {health.message}",
            )
        return SetupUiResult(
            ok=True,
            provider_id=provider_id,
            tested=True,
            message=f"Saved setup and provider test passed: {health.message}",
        )

    return SetupUiResult(
        ok=True,
        provider_id=provider_id,
        tested=False,
        message="Saved setup. Run `the-matrix providers test` when you are ready.",
    )


class _SetupServer(ThreadingHTTPServer):
    allow_reuse_address = False


def _handler_factory(
    paths: MatrixPaths,
    vault: MemoryVault,
    store: RuntimeStore,
    token: str,
    keymaker_factory: Callable[[], Keymaker],
):
    class SetupHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if not self._token_ok():
                self._send_html(HTTPStatus.FORBIDDEN, _message_page("Forbidden", "Invalid token."))
                return
            self._send_html(
                HTTPStatus.OK,
                render_setup_form(token, detections=detect_local_providers(timeout_seconds=0.5)),
            )

        def do_POST(self) -> None:
            if not self._token_ok():
                self._send_html(HTTPStatus.FORBIDDEN, _message_page("Forbidden", "Invalid token."))
                return
            parsed = urlparse(self.path)
            if parsed.path != "/save":
                self._send_html(HTTPStatus.NOT_FOUND, _message_page("Not found", "Unknown route."))
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_html(
                    HTTPStatus.BAD_REQUEST,
                    _message_page("Bad request", "Content-Length must be a number."),
                )
                return
            if length > MAX_SETUP_BODY_BYTES:
                self._send_html(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    _message_page("Too large", "Setup form payload is too large."),
                )
                return
            raw = self.rfile.read(length).decode("utf-8")
            form = {key: values[-1] for key, values in parse_qs(raw).items()}
            result = apply_setup_form(form, paths, vault, store, keymaker_factory())
            if not result.ok:
                self._send_html(
                    HTTPStatus.BAD_REQUEST,
                    render_setup_form(
                        token,
                        error=result.message,
                        detections=detect_local_providers(timeout_seconds=0.5),
                    ),
                )
                return
            self._send_html(HTTPStatus.OK, _message_page("Setup saved", result.message))
            Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, format: str, *args: object) -> None:
            return

        def _token_ok(self) -> bool:
            query = parse_qs(urlparse(self.path).query)
            supplied = query.get("token", [""])[-1]
            return hmac.compare_digest(supplied, token)

        def _send_html(self, status: HTTPStatus, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

    return SetupHandler


def render_setup_form(
    token: str,
    error: str | None = None,
    detections: list[ProviderDetection] | None = None,
) -> str:
    providers = provider_catalog()
    detection_by_id = {detection.provider_id: detection for detection in detections or []}
    provider_options = "\n".join(
        f'<option value="{escape(profile.provider_id)}">{escape(profile.display_name)}'
        f" ({escape(profile.kind.value)})</option>"
        for profile in providers
    )
    model_options = "\n".join(
        f'<option value="{escape(model)}"></option>'
        for profile in providers
        for model in profile.suggested_models
    )
    auth_options = "\n".join(
        f'<option value="{mode.value}">{mode.value}</option>' for mode in AuthMode
    )
    provider_notes = "".join(
        _provider_note(profile, detection_by_id.get(profile.provider_id))
        for profile in providers
    )
    provider_json = _provider_setup_json(providers, detection_by_id)
    error_html = f'<div class="error">{escape(error)}</div>' if error else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>The Matrix Setup</title>
  <style>
    body {{
      margin: 0;
      background: #f7f8f5;
      color: #1f2523;
      font: 14px/1.45 "Segoe UI", system-ui, sans-serif;
    }}
    main {{
      width: min(860px, calc(100% - 32px));
      margin: 0 auto;
      padding: 28px 0 44px;
    }}
    h1 {{ margin: 0 0 4px; font-size: 30px; letter-spacing: 0; }}
    h2 {{ margin: 24px 0 10px; font-size: 18px; }}
    p {{ margin: 0 0 12px; color: #65706b; }}
    form, .notes {{
      background: white;
      border: 1px solid #dce1db;
      border-radius: 8px;
      padding: 18px;
    }}
    label {{ display: grid; gap: 6px; margin: 12px 0; font-weight: 600; }}
    input, select {{
      width: 100%;
      border: 1px solid #cfd6d0;
      border-radius: 6px;
      padding: 10px;
      font: inherit;
      background: white;
    }}
    .hint {{
      color: #65706b;
      font-size: 13px;
      font-weight: 400;
    }}
    .provider-card {{
      border: 1px solid #dce1db;
      border-radius: 8px;
      padding: 12px;
      background: #fbfcfa;
      margin: 12px 0;
    }}
    .provider-card strong {{ display: block; margin-bottom: 4px; }}
    .hidden {{ display: none; }}
    .row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
    .check {{ display: flex; gap: 8px; align-items: center; font-weight: 500; }}
    .check input {{ width: auto; }}
    button {{
      margin-top: 14px;
      border: 0;
      border-radius: 6px;
      padding: 11px 14px;
      background: #1f7a4d;
      color: white;
      font-weight: 700;
      cursor: pointer;
    }}
    .error {{
      border: 1px solid #e6c0bd;
      background: #fff6f4;
      color: #9b2f2f;
      border-radius: 6px;
      padding: 10px;
      margin: 14px 0;
    }}
    .note {{
      border-top: 1px solid #dce1db;
      padding-top: 10px;
      margin-top: 10px;
    }}
    .note:first-child {{ border-top: 0; padding-top: 0; margin-top: 0; }}
    code {{ color: #0d6b72; overflow-wrap: anywhere; }}
    @media (max-width: 760px) {{ .row {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <main>
    <h1>The Matrix Setup</h1>
    <p>This guide sets up the local memory folder, model connection, and safe permission defaults. It runs only on this PC.</p>
    {error_html}
    <form method="post" action="/save?token={escape(token)}">
      <h2>1. Choose How The Matrix Thinks</h2>
      <label>Model provider
        <select id="provider_id" name="provider_id" required>{provider_options}</select>
        <span class="hint">If a local model app is already running, it will be selected for you.</span>
      </label>
      <div id="provider_card" class="provider-card"></div>
      <div class="row">
        <label>Model
          <input id="model" name="model" list="models" placeholder="Enter a model id" required>
          <datalist id="models">{model_options}</datalist>
          <span id="model_hint" class="hint"></span>
        </label>
        <label>Sign-in method
          <select id="auth_mode" name="auth_mode">{auth_options}</select>
          <span id="auth_hint" class="hint"></span>
        </label>
      </div>
      <label id="api_key_row">API key or local token
        <input id="api_key" name="api_key" type="password" autocomplete="off" placeholder="Only stored through Keymaker">
        <span id="api_key_hint" class="hint"></span>
      </label>
      <label>Provider address
        <input id="base_url" name="base_url" placeholder="Use provider default unless you need a custom endpoint">
        <span id="base_url_hint" class="hint"></span>
      </label>
      <h2>2. Choose Safe Defaults</h2>
      <div class="row">
        <label>Data sharing
          <select name="privacy_mode">
            <option value="ask_each_time">Ask me when cloud use matters</option>
            <option value="cloud_allowed">Allow configured cloud provider</option>
            <option value="local_only">Local models only</option>
          </select>
        </label>
        <label>Changing files
          <select name="file_change_consent">
            <option value="ask_each_time">Ask before changing files</option>
            <option value="allow_always">Allow approved agents to change files</option>
          </select>
        </label>
      </div>
      <label class="check"><input name="guarded_shell_enabled" type="checkbox" checked> Allow safe read/check commands automatically</label>
      <label class="check"><input name="test_provider" type="checkbox" checked> Test the connection before finishing</label>
      <button type="submit">Finish setup</button>
    </form>
    <h2>Provider Help</h2>
    <div class="notes">{provider_notes}</div>
    <script id="provider-data" type="application/json">{provider_json}</script>
    <script>
      const providers = JSON.parse(document.getElementById("provider-data").textContent);
      const byId = Object.fromEntries(providers.map((provider) => [provider.provider_id, provider]));
      const providerSelect = document.getElementById("provider_id");
      const modelInput = document.getElementById("model");
      const authSelect = document.getElementById("auth_mode");
      const apiKeyRow = document.getElementById("api_key_row");
      const apiKeyInput = document.getElementById("api_key");
      const baseUrlInput = document.getElementById("base_url");
      const providerCard = document.getElementById("provider_card");
      const modelHint = document.getElementById("model_hint");
      const authHint = document.getElementById("auth_hint");
      const apiKeyHint = document.getElementById("api_key_hint");
      const baseUrlHint = document.getElementById("base_url_hint");
      const detectedProvider = providers.find((provider) => provider.detected_reachable);
      if (detectedProvider) providerSelect.value = detectedProvider.provider_id;

      function preferredAuth(provider) {{
        if (provider.auth_modes.includes("api_key")) return "api_key";
        if (provider.auth_modes.includes("none")) return "none";
        if (provider.auth_modes.includes("local_token")) return "local_token";
        return provider.auth_modes[0] || "none";
      }}

      function syncProvider() {{
        const provider = byId[providerSelect.value];
        if (!provider) return;
        const models = provider.detected_models.length ? provider.detected_models : provider.suggested_models;
        modelInput.value = models[0] || "default";
        baseUrlInput.value = provider.default_base_url || "";
        authSelect.innerHTML = "";
        for (const mode of provider.auth_modes) {{
          const option = document.createElement("option");
          option.value = mode;
          option.textContent = mode;
          authSelect.appendChild(option);
        }}
        authSelect.value = preferredAuth(provider);
        providerCard.innerHTML = `
          <strong></strong>
          <p class="setup-hint"></p>
          <p><span class="hint data-boundary"></span></p>
        `;
        providerCard.querySelector("strong").textContent = `${{provider.display_name}} (${{provider.kind}})`;
        providerCard.querySelector(".setup-hint").textContent = provider.detected_reachable
          ? `Detected on this PC. ${{provider.detected_message}}`
          : provider.setup_hint;
        providerCard.querySelector(".data-boundary").textContent = provider.data_boundary;
        modelHint.textContent = models.length
          ? `Examples: ${{models.join(", ")}}`
          : "Enter the model id expected by this endpoint.";
        baseUrlHint.textContent = provider.default_base_url
          ? `Default: ${{provider.default_base_url}}`
          : "Required for custom endpoints.";
        syncAuth();
      }}

      function syncAuth() {{
        const mode = authSelect.value;
        const needsSecret = mode === "api_key" || mode === "local_token";
        apiKeyRow.classList.toggle("hidden", !needsSecret);
        apiKeyInput.required = needsSecret;
        apiKeyHint.textContent = needsSecret
          ? "Stored in your operating system credential store. It is not written to memory notes or logs."
          : "No secret is needed for this provider mode.";
        authHint.textContent = mode === "oauth"
          ? "OAuth is listed as provider capability, but setup is not wired in this version."
          : "The Python backend validates this choice before saving.";
      }}

      providerSelect.addEventListener("change", syncProvider);
      authSelect.addEventListener("change", syncAuth);
      syncProvider();
    </script>
  </main>
</body>
</html>
"""


def _provider_note(profile: ProviderProfile, detection: ProviderDetection | None = None) -> str:
    auth = ", ".join(mode.value for mode in profile.auth_modes)
    models = ", ".join(profile.suggested_models) if profile.suggested_models else "custom"
    detection_html = ""
    if detection is not None:
        status = "Detected on this PC" if detection.reachable else "Not detected on this PC"
        detection_html = f"<p>{escape(status)}: {escape(detection.message)}</p>"
    return (
        '<div class="note">'
        f"<strong>{escape(profile.display_name)}</strong>"
        f"<p>{escape(profile.setup_hint)}</p>"
        f"{detection_html}"
        f"<p>Auth: <code>{escape(auth)}</code></p>"
        f"<p>Model examples: <code>{escape(models)}</code></p>"
        f"<p>{escape(profile.data_boundary)}</p>"
        "</div>"
    )


def _provider_setup_json(
    profiles: list[ProviderProfile],
    detection_by_id: dict[str, ProviderDetection] | None = None,
) -> str:
    detection_by_id = detection_by_id or {}
    payload = [
        {
            "provider_id": profile.provider_id,
            "display_name": profile.display_name,
            "kind": profile.kind.value,
            "auth_modes": [mode.value for mode in profile.auth_modes],
            "suggested_models": profile.suggested_models,
            "detected_reachable": _detected_reachable(profile.provider_id, detection_by_id),
            "detected_models": _detected_models(profile.provider_id, detection_by_id),
            "detected_message": _detected_message(profile.provider_id, detection_by_id),
            "default_base_url": profile.default_base_url,
            "setup_hint": profile.setup_hint,
            "data_boundary": profile.data_boundary,
        }
        for profile in profiles
    ]
    return (
        json.dumps(payload)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _detected_reachable(
    provider_id: str,
    detection_by_id: dict[str, ProviderDetection],
) -> bool:
    detection = detection_by_id.get(provider_id)
    return bool(detection and detection.reachable)


def _detected_models(
    provider_id: str,
    detection_by_id: dict[str, ProviderDetection],
) -> list[str]:
    detection = detection_by_id.get(provider_id)
    return detection.models if detection else []


def _detected_message(
    provider_id: str,
    detection_by_id: dict[str, ProviderDetection],
) -> str:
    detection = detection_by_id.get(provider_id)
    return detection.message if detection else ""


def _message_page(title: str, message: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body {{ margin: 0; background: #f7f8f5; color: #1f2523; font: 15px/1.45 "Segoe UI", system-ui, sans-serif; }}
    main {{ width: min(720px, calc(100% - 32px)); margin: 0 auto; padding: 42px 0; }}
    section {{ background: white; border: 1px solid #dce1db; border-radius: 8px; padding: 18px; }}
    h1 {{ margin: 0 0 8px; }}
    p {{ margin: 0; color: #65706b; }}
  </style>
</head>
<body><main><section><h1>{escape(title)}</h1><p>{escape(message)}</p></section></main></body>
</html>
"""
