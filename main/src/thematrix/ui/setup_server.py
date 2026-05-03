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
    auth_options = "\n".join(
        f'<option value="{mode.value}">{mode.value}</option>' for mode in AuthMode
    )
    provider_notes = "".join(
        _provider_note(profile, detection_by_id.get(profile.provider_id))
        for profile in providers
    )
    provider_json = _provider_setup_json(providers, detection_by_id)
    error_html = f'<div class="error">{escape(error)}</div>' if error else ""
    detected_count = sum(1 for d in (detections or []) if d.reachable)
    if detections is None:
        detected_line = "skipping local provider scan"
    elif detected_count == 0:
        detected_line = "no local providers detected"
    elif detected_count == 1:
        detected_line = "1 local provider detected"
    else:
        detected_line = f"{detected_count} local providers detected"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>The Matrix Setup</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=VT323&display=swap" rel="stylesheet">
  <style>
    :root {{
      color-scheme: dark;
      --void: #000000;
      --panel-bg: rgba(0, 14, 4, 0.78);
      --panel-edge: rgba(0, 255, 65, 0.10);
      --phosphor: #00b341;
      --phosphor-dim: #1f5530;
      --phosphor-title: #7cff9d;
      --phosphor-deep: #0a2c14;
      --phosphor-bright: #00ff41;
      --phosphor-white: #d4ffe2;
      --phosphor-amber: #ffb94d;
      --phosphor-red: #ff003c;
      --line: rgba(0, 255, 65, 0.14);
      --line-soft: rgba(0, 255, 65, 0.06);
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
      width: 100vw; height: 100vh;
      z-index: 0;
      opacity: 0.28;
      pointer-events: none;
    }}
    body::before {{
      content: '';
      position: fixed;
      inset: 0;
      background: radial-gradient(ellipse 95% 80% at 50% 45%,
        transparent 0%,
        rgba(0, 0, 0, 0.2) 60%,
        rgba(0, 0, 0, 0.55) 100%);
      pointer-events: none;
      z-index: 1;
    }}
    body::after {{
      content: '';
      position: fixed;
      inset: 0;
      background: repeating-linear-gradient(
        0deg,
        transparent 0px,
        transparent 2px,
        rgba(0, 0, 0, 0.22) 3px,
        transparent 4px
      );
      pointer-events: none;
      z-index: 999;
      mix-blend-mode: multiply;
    }}
    @keyframes blink {{ 0%, 49% {{ opacity: 1; }} 50%, 100% {{ opacity: 0; }} }}
    @keyframes wake {{
      from {{ opacity: 0; transform: translateY(8px); filter: blur(3px); }}
      to {{ opacity: 1; transform: translateY(0); filter: blur(0); }}
    }}
    @keyframes bootLine {{
      from {{ opacity: 0; transform: translateX(-12px); }}
      to {{ opacity: 1; transform: translateX(0); }}
    }}
    @keyframes pulseDot {{
      0%, 100% {{ box-shadow: 0 0 4px var(--phosphor-bright), 0 0 8px rgba(0, 255, 65, 0.5); opacity: 1; }}
      50% {{ box-shadow: 0 0 2px var(--phosphor-bright); opacity: 0.45; }}
    }}
    @keyframes phosphorPulse {{
      0%, 100% {{ text-shadow: 0 0 4px rgba(0, 255, 65, 0.95), 0 0 18px rgba(0, 255, 65, 0.55), 0 0 36px rgba(0, 255, 65, 0.25); }}
      50% {{ text-shadow: 0 0 6px rgba(0, 255, 65, 1), 0 0 24px rgba(0, 255, 65, 0.7), 0 0 48px rgba(0, 255, 65, 0.35); }}
    }}
    main {{
      position: relative;
      z-index: 2;
      width: min(900px, calc(100% - 48px));
      margin: 0 auto;
      padding: 36px 0 56px;
    }}
    /* BOOT LOG */
    .boot-log {{
      margin-bottom: 22px;
      font-size: 13px;
      color: var(--phosphor-title);
      letter-spacing: 0.5px;
      line-height: 1.9;
    }}
    .boot-log p {{ margin: 0; opacity: 0; animation: bootLine 380ms ease-out both; }}
    .boot-log p::before {{ content: '> '; color: var(--phosphor); }}
    .boot-log p:nth-child(1) {{ animation-delay: 80ms; }}
    .boot-log p:nth-child(2) {{ animation-delay: 240ms; }}
    .boot-log p:nth-child(3) {{ animation-delay: 400ms; }}
    .boot-log p:nth-child(4) {{ animation-delay: 560ms; color: var(--phosphor); }}
    /* HEADER */
    header {{
      padding-bottom: 22px;
      margin-bottom: 26px;
      border-bottom: 1px solid var(--line);
      animation: wake 700ms ease-out 600ms both;
    }}
    h1 {{
      font-family: "VT323", "Cascadia Mono", monospace;
      font-size: clamp(56px, 8vw, 96px);
      line-height: 0.85;
      margin: 0 0 12px;
      letter-spacing: 6px;
      color: var(--phosphor-bright);
      text-transform: uppercase;
      animation: phosphorPulse 4.5s ease-in-out infinite;
    }}
    h1::before {{ content: '> '; color: var(--phosphor-title); letter-spacing: 0; }}
    h1::after {{
      content: '_';
      animation: blink 1.05s step-end infinite;
      color: var(--phosphor-bright);
      margin-left: 4px;
    }}
    .hud-status {{
      display: flex;
      flex-wrap: wrap;
      gap: 18px;
      font-size: 12px;
      letter-spacing: 1.5px;
      text-transform: uppercase;
      color: var(--phosphor-title);
    }}
    .hud-status span {{ display: inline-flex; align-items: center; gap: 8px; }}
    .hud-status .dot {{
      width: 6px; height: 6px;
      border-radius: 50%;
      background: var(--phosphor-bright);
      animation: pulseDot 2.4s ease-in-out infinite;
    }}
    .hud-status .v {{ color: var(--phosphor-bright); }}
    .lede {{
      margin: 14px 0 0;
      font-size: 14px;
      color: var(--phosphor-title);
      letter-spacing: 0.3px;
    }}
    /* SECTION HEADINGS */
    h2 {{
      margin: 28px 0 14px;
      font-size: 13px;
      font-weight: normal;
      text-transform: uppercase;
      letter-spacing: 2.5px;
      color: var(--phosphor-title);
      padding-bottom: 8px;
      border-bottom: 1px dashed var(--line);
    }}
    h2::before {{ content: '// '; color: var(--phosphor-title); }}
    p {{ margin: 0; }}
    /* FORM AS HUD CONSOLE */
    form, .notes {{
      position: relative;
      background: var(--panel-bg);
      border: 1px solid var(--panel-edge);
      border-left: 2px solid var(--phosphor-dim);
      padding: 28px 22px 22px;
      animation: wake 700ms ease-out 800ms both;
    }}
    form::before, .notes::before {{
      content: '◤';
      position: absolute;
      top: 6px; left: 8px;
      color: var(--phosphor-title);
      font-size: 10px;
    }}
    form::after, .notes::after {{
      content: '◢';
      position: absolute;
      bottom: 6px; right: 8px;
      color: var(--phosphor-dim);
      font-size: 10px;
    }}
    .notes {{ animation-delay: 1000ms; }}
    label {{
      display: grid;
      gap: 8px;
      margin: 16px 0;
      font-size: 12px;
      letter-spacing: 2px;
      text-transform: uppercase;
      color: var(--phosphor-dim);
    }}
    input, select {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 0;
      padding: 12px 14px;
      font: inherit;
      font-size: 15px;
      background: rgba(0, 8, 2, 0.8);
      color: var(--phosphor-bright);
      caret-color: var(--phosphor-bright);
      letter-spacing: 0.3px;
      text-transform: none;
      transition: border-color 150ms ease, box-shadow 150ms ease;
    }}
    input:focus, select:focus {{
      border-color: var(--phosphor-bright);
      box-shadow: 0 0 0 1px var(--phosphor-bright), 0 0 14px rgba(0, 255, 65, 0.25);
      outline: none;
    }}
    input::placeholder {{ color: rgba(31, 85, 48, 0.7); text-transform: none; letter-spacing: 0.5px; }}
    select {{
      appearance: none;
      -webkit-appearance: none;
      background-image: linear-gradient(45deg, transparent 50%, var(--phosphor) 50%),
                        linear-gradient(135deg, var(--phosphor) 50%, transparent 50%);
      background-position: calc(100% - 18px) 50%, calc(100% - 13px) 50%;
      background-size: 5px 5px, 5px 5px;
      background-repeat: no-repeat;
      padding-right: 32px;
    }}
    select option {{ background: #000a00; color: var(--phosphor); }}
    .hint {{
      color: var(--phosphor-title);
      font-size: 13px;
      letter-spacing: 0.2px;
      text-transform: none;
      font-weight: normal;
      line-height: 1.55;
    }}
    .hint::before {{ content: '↳ '; color: var(--phosphor-title); }}
    /* PROVIDER CARD — live HUD readout */
    .provider-card {{
      position: relative;
      border: 1px solid var(--line);
      background: rgba(0, 22, 8, 0.55);
      padding: 18px 16px 14px;
      margin: 18px 0;
    }}
    .provider-card::before {{
      content: '◤ live readout';
      position: absolute;
      top: -8px; left: 12px;
      background: var(--void);
      padding: 0 8px;
      font-size: 11px;
      letter-spacing: 2px;
      text-transform: uppercase;
      color: var(--phosphor-title);
    }}
    .provider-card::after {{
      content: '';
      position: absolute;
      bottom: 6px; right: 6px;
      width: 12px; height: 12px;
      border-bottom: 1px solid var(--phosphor-dim);
      border-right: 1px solid var(--phosphor-dim);
    }}
    .provider-card strong {{
      display: block;
      margin-bottom: 10px;
      color: var(--phosphor-bright);
      text-shadow: 0 0 6px rgba(0, 255, 65, 0.5);
      font-weight: normal;
      font-size: 17px;
      letter-spacing: 0.8px;
    }}
    .provider-card p {{ margin: 6px 0; }}
    .provider-card .setup-hint {{ color: var(--phosphor); font-size: 15px; line-height: 1.55; }}
    .provider-card .data-boundary {{ color: var(--phosphor-title); font-size: 13px; }}
    .hidden {{ display: none; }}
    .row {{ display: grid; grid-template-columns: minmax(260px, 1fr) minmax(260px, 1fr); gap: 18px; }}
    .check {{
      display: flex;
      gap: 12px;
      align-items: center;
      font-size: 14px;
      color: var(--phosphor);
      text-transform: none;
      letter-spacing: 0.2px;
      cursor: pointer;
    }}
    .check input {{
      width: 16px; height: 16px;
      accent-color: var(--phosphor-bright);
      cursor: pointer;
    }}
    /* SUBMIT BUTTON — chunky HUD action */
    button {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      margin-top: 22px;
      border: 1px solid var(--phosphor-bright);
      border-radius: 0;
      padding: 14px 28px;
      background: transparent;
      color: var(--phosphor-bright);
      font: inherit;
      font-family: "VT323", "Cascadia Mono", monospace;
      font-size: 22px;
      cursor: pointer;
      text-transform: uppercase;
      letter-spacing: 4px;
      box-shadow: 0 0 12px rgba(0, 255, 65, 0.3),
                  inset 0 0 12px rgba(0, 255, 65, 0.04);
      text-shadow: 0 0 8px rgba(0, 255, 65, 0.7);
      transition: background 200ms ease, box-shadow 200ms ease, transform 100ms ease;
      position: relative;
    }}
    button::before {{ content: '▸'; font-size: 18px; }}
    button:hover {{
      background: rgba(0, 255, 65, 0.1);
      box-shadow: 0 0 24px rgba(0, 255, 65, 0.5),
                  inset 0 0 24px rgba(0, 255, 65, 0.08);
    }}
    button:active {{ transform: translateY(1px); }}
    .error {{
      position: relative;
      border: 1px solid rgba(255, 0, 60, 0.5);
      background: rgba(40, 0, 8, 0.7);
      color: var(--phosphor-red);
      padding: 12px 14px 12px 36px;
      margin: 18px 0;
      text-shadow: 0 0 6px rgba(255, 0, 60, 0.4);
      letter-spacing: 0.5px;
      animation: wake 380ms ease-out both;
    }}
    .error::before {{
      content: '⚠';
      position: absolute;
      top: 50%; left: 14px;
      transform: translateY(-50%);
      font-size: 14px;
    }}
    .note {{
      border-top: 1px dashed var(--line);
      padding-top: 12px;
      margin-top: 12px;
    }}
    .note:first-child {{ border-top: 0; padding-top: 0; margin-top: 0; }}
    .note strong {{
      display: block;
      margin-bottom: 6px;
      color: var(--phosphor-bright);
      text-shadow: 0 0 4px rgba(0, 255, 65, 0.4);
      font-weight: normal;
      font-size: 16px;
      letter-spacing: 0.8px;
    }}
    .note p {{ margin: 5px 0; font-size: 14px; color: var(--phosphor-title); line-height: 1.55; }}
    .note p:nth-child(2) {{ color: var(--phosphor); }}
    code {{
      font-family: "Share Tech Mono", "Cascadia Mono", monospace;
      color: var(--phosphor-bright);
      text-shadow: 0 0 6px rgba(0, 255, 65, 0.4);
      overflow-wrap: anywhere;
    }}
    .notes {{
      display: grid;
      gap: 14px;
    }}
    .provider-registry {{
      margin-top: 28px;
    }}
    .provider-registry summary {{
      cursor: pointer;
      list-style: none;
      color: var(--phosphor-bright);
      font-size: 15px;
      letter-spacing: 2px;
      text-transform: uppercase;
      text-shadow: 0 0 6px rgba(0, 255, 65, 0.4);
    }}
    .provider-registry summary::-webkit-details-marker {{ display: none; }}
    .provider-registry summary::before {{
      content: '▸ ';
      color: var(--phosphor-title);
    }}
    .provider-registry[open] summary::before {{ content: '▾ '; }}
    .provider-registry .provider-notes-list {{
      display: grid;
      gap: 14px;
      margin-top: 16px;
    }}
    .footer-bar {{
      margin-top: 26px;
      padding-top: 14px;
      border-top: 1px solid var(--line);
      display: flex;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
      font-size: 11px;
      color: var(--phosphor-title);
      letter-spacing: 2px;
      text-transform: uppercase;
      opacity: 0;
      animation: wake 600ms ease-out 1500ms both;
    }}
    @media (max-width: 760px) {{
      .row {{ grid-template-columns: 1fr; }}
      main {{ width: min(900px, calc(100% - 28px)); }}
      h1 {{ font-size: 52px; letter-spacing: 3px; }}
    }}
  </style>
</head>
<body>
  <canvas id="matrix-rain" aria-hidden="true"></canvas>
  <main>
    <div class="boot-log" aria-hidden="true">
      <p>opening secure channel :: 127.0.0.1</p>
      <p>scanning environment for providers</p>
      <p>{escape(detected_line)}</p>
      <p>awaiting operator</p>
    </div>
    <header>
      <h1>The Matrix</h1>
      <div class="hud-status">
        <span><span class="dot"></span>node<span class="v">::local</span></span>
        <span>channel<span class="v">::secure</span></span>
        <span>vault<span class="v">::pending</span></span>
        <span>keymaker<span class="v">::ready</span></span>
      </div>
      <p class="lede">Configure the local memory vault, model link, and safety protocols. This session is bound to this node only.</p>
    </header>
    {error_html}
    <form method="post" action="/save?token={escape(token)}">
      <h2>01 / Model Interface</h2>
      <label>Model provider
        <select id="provider_id" name="provider_id" required>{provider_options}</select>
        <span class="hint">If a local model app is already running, it will be selected for you.</span>
      </label>
      <div id="provider_card" class="provider-card"></div>
      <div class="row">
        <label>Model
          <input id="model" name="model" placeholder="Enter a model id" required>
          <span id="model_hint" class="hint"></span>
        </label>
        <label id="auth_mode_row">Sign-in method
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
      <h2>02 / Safety Protocols</h2>
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
      <button type="submit">Initialize Connection</button>
    </form>
    <details class="notes provider-registry">
      <summary>Provider Registry</summary>
      <div class="provider-notes-list">{provider_notes}</div>
    </details>
    <div class="footer-bar">
      <span>// the matrix has you</span>
      <span>// follow the white rabbit &nbsp;▌</span>
    </div>
    <script id="provider-data" type="application/json">{provider_json}</script>
    <script>
      const providers = JSON.parse(document.getElementById("provider-data").textContent);
      const byId = Object.fromEntries(providers.map((provider) => [provider.provider_id, provider]));
      const providerSelect = document.getElementById("provider_id");
      const modelInput = document.getElementById("model");
      const authSelect = document.getElementById("auth_mode");
      const authModeRow = document.getElementById("auth_mode_row");
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
        const modes = setupAuthModes(provider);
        if (modes.includes("api_key")) return "api_key";
        if (modes.includes("none")) return "none";
        if (modes.includes("local_token")) return "local_token";
        return modes[0] || "none";
      }}

      function setupAuthModes(provider) {{
        const modes = provider.auth_modes.filter((mode) => mode !== "oauth");
        return modes.length ? modes : provider.auth_modes;
      }}

      function syncProvider() {{
        const provider = byId[providerSelect.value];
        if (!provider) return;
        const models = provider.detected_models.length ? provider.detected_models : provider.suggested_models;
        const authModes = setupAuthModes(provider);
        modelInput.value = models[0] || "default";
        baseUrlInput.value = provider.default_base_url || "";
        authSelect.innerHTML = "";
        for (const mode of authModes) {{
          const option = document.createElement("option");
          option.value = mode;
          option.textContent = mode;
          authSelect.appendChild(option);
        }}
        authModeRow.classList.toggle("hidden", authModes.length <= 1);
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
          if (Math.random() > 0.975) {{
            ctx.fillStyle = '#d4ffe2';
            ctx.shadowColor = '#00ff41';
            ctx.shadowBlur = 10;
          }} else {{
            ctx.fillStyle = '#00b341';
            ctx.shadowBlur = 0;
          }}
          ctx.fillText(ch, i * fontSize, y);
          if (y > canvas.height && Math.random() > 0.972) drops[i] = 0;
          drops[i]++;
        }}
      }}
      setInterval(draw, 60);
    }})();
  </script>
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
  <title>The Matrix {escape(title)}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=VT323&display=swap" rel="stylesheet">
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; }}
    body {{
      background: #000000;
      color: #00b341;
      font: 14px/1.55 "Share Tech Mono", "Cascadia Mono", "Courier New", monospace;
      min-height: 100vh;
      overflow-x: hidden;
    }}
    body::before {{
      content: '';
      position: fixed; inset: 0;
      background: radial-gradient(ellipse 70% 60% at 50% 50%,
        transparent 0%, rgba(0,0,0,0.45) 70%, rgba(0,0,0,0.9) 100%);
      pointer-events: none; z-index: 1;
    }}
    body::after {{
      content: '';
      position: fixed; inset: 0;
      background: repeating-linear-gradient(0deg,
        transparent 0px, transparent 2px,
        rgba(0,0,0,0.22) 3px, transparent 4px);
      pointer-events: none; z-index: 999;
      mix-blend-mode: multiply;
    }}
    @keyframes blink {{ 0%, 49% {{ opacity: 1; }} 50%, 100% {{ opacity: 0; }} }}
    @keyframes wake {{
      from {{ opacity: 0; transform: translateY(8px); filter: blur(3px); }}
      to {{ opacity: 1; transform: translateY(0); filter: blur(0); }}
    }}
    main {{
      position: relative; z-index: 2;
      width: min(720px, calc(100% - 48px));
      margin: 0 auto;
      padding: 80px 0 48px;
    }}
    section {{
      position: relative;
      background: rgba(0, 14, 4, 0.78);
      border: 1px solid rgba(0, 255, 65, 0.10);
      border-left: 2px solid #00ff41;
      padding: 36px 28px 28px;
      animation: wake 600ms ease-out both;
    }}
    section::before {{
      content: '◤';
      position: absolute; top: 8px; left: 10px;
      color: #1f5530; font-size: 12px;
    }}
    section::after {{
      content: '◢';
      position: absolute; bottom: 8px; right: 10px;
      color: #1f5530; font-size: 12px;
    }}
    h1 {{
      margin: 0 0 14px;
      font-family: "VT323", "Cascadia Mono", monospace;
      font-size: 56px;
      line-height: 0.9;
      letter-spacing: 4px;
      color: #00ff41;
      text-transform: uppercase;
      text-shadow: 0 0 6px rgba(0, 255, 65, 0.95), 0 0 22px rgba(0, 255, 65, 0.45);
    }}
    h1::before {{ content: '> '; color: #1f5530; letter-spacing: 0; }}
    h1::after {{
      content: '_';
      animation: blink 1.05s step-end infinite;
      color: #00ff41;
      margin-left: 4px;
    }}
    p {{
      margin: 0;
      color: #00b341;
      font-size: 14px;
      letter-spacing: 0.3px;
    }}
    p::before {{ content: '> '; color: #1f5530; }}
    .footer-bar {{
      margin-top: 22px;
      font-size: 10px;
      color: #1f5530;
      letter-spacing: 2.5px;
      text-transform: uppercase;
      text-align: center;
      opacity: 0;
      animation: wake 500ms ease-out 600ms both;
    }}
  </style>
</head>
<body>
  <main>
    <section>
      <h1>{escape(title)}</h1>
      <p>{escape(message)}</p>
    </section>
    <div class="footer-bar">// the matrix has you &nbsp;&middot;&nbsp; follow the white rabbit ▌</div>
  </main>
</body>
</html>
"""
