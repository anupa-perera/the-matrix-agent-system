from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
from secrets import token_urlsafe
from threading import Lock, Timer
from urllib.parse import parse_qs, urlparse
import webbrowser

from thematrix.config import MatrixPaths
from thematrix.memory import MemoryVault, RuntimeStore
from thematrix.schemas import MatrixRunResult
from thematrix.ui.dashboard import write_dashboard

MAX_APP_BODY_BYTES = 64 * 1024
DEFAULT_APP_TIMEOUT_SECONDS = 60 * 60


@dataclass(frozen=True)
class AppUiResponse:
    result: MatrixRunResult | None = None
    error: str | None = None


def serve_app_ui(
    paths: MatrixPaths,
    vault: MemoryVault,
    store: RuntimeStore,
    request_runner: Callable[[str], MatrixRunResult],
    port: int = 0,
    open_browser: bool = True,
    url_callback: Callable[[str], None] | None = None,
    timeout_seconds: int = DEFAULT_APP_TIMEOUT_SECONDS,
) -> str:
    token = token_urlsafe(24)
    run_lock = Lock()
    server = _AppServer(
        ("127.0.0.1", port),
        _handler_factory(paths, vault, store, token, request_runner, run_lock),
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


class _AppServer(ThreadingHTTPServer):
    allow_reuse_address = False


def _handler_factory(
    paths: MatrixPaths,
    vault: MemoryVault,
    store: RuntimeStore,
    token: str,
    request_runner: Callable[[str], MatrixRunResult],
    run_lock: Lock,
):
    class AppHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if not self._token_ok():
                self._send_html(HTTPStatus.FORBIDDEN, _message_page("Forbidden", "Invalid token."))
                return
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send_html(HTTPStatus.OK, render_app_page(paths, store, token))
                return
            if parsed.path == "/dashboard":
                dashboard_path = write_dashboard(paths, store)
                self._send_html(
                    HTTPStatus.OK,
                    _message_page("Dashboard Updated", f"Dashboard written: {dashboard_path}"),
                )
                return
            self._send_html(HTTPStatus.NOT_FOUND, _message_page("Not Found", "Unknown route."))

        def do_POST(self) -> None:
            if not self._token_ok():
                self._send_html(HTTPStatus.FORBIDDEN, _message_page("Forbidden", "Invalid token."))
                return
            parsed = urlparse(self.path)
            if parsed.path != "/ask":
                self._send_html(HTTPStatus.NOT_FOUND, _message_page("Not Found", "Unknown route."))
                return
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
                return
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
                return

            raw = self.rfile.read(length).decode("utf-8")
            form = {key: values[-1] for key, values in parse_qs(raw).items()}
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
                        AppUiResponse(error="A mission is already running. Try again in a moment."),
                    ),
                )
                return
            try:
                result = request_runner(user_request)
            except Exception as exc:
                response = AppUiResponse(error=f"Mission failed: {exc}")
            else:
                response = AppUiResponse(result=result)
            finally:
                run_lock.release()
            self._send_html(HTTPStatus.OK, render_app_page(paths, store, token, response))

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

    return AppHandler


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
    result_html = _result_panel(response)
    recent_html = _recent_runs_panel(store)
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
      <form method="post" action="/ask?token={escape(token)}">
        <label>What do you want the agents to do?
          <textarea name="request" placeholder="Create a reusable research agent for comparing AI tools" required></textarea>
        </label>
        <div class="actions">
          <button type="submit">Run Mission</button>
          <a class="button-link" href="/dashboard?token={escape(token)}">Refresh Dashboard</a>
        </div>
      </form>
    </section>
    {result_html}
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
</body>
</html>
"""


def _result_panel(response: AppUiResponse) -> str:
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


def _recent_runs_panel(store: RuntimeStore) -> str:
    runs = store.list_run_records(limit=5)
    items = []
    for run in runs:
        items.append(
            f"""
        <div class="item">
          <p><code>{escape(run["run_id"])}</code></p>
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
