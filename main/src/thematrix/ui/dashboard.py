from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from thematrix.config import MatrixPaths
from thematrix.memory import RuntimeStore


def write_dashboard(paths: MatrixPaths, store: RuntimeStore) -> str:
    paths.dashboard_dir.mkdir(parents=True, exist_ok=True)
    html = render_dashboard_html(paths, store)
    paths.dashboard_file.write_text(html, encoding="utf-8")
    return str(paths.dashboard_file)


def render_dashboard_html(paths: MatrixPaths, store: RuntimeStore) -> str:
    counts = store.overview_counts()
    provider_config = store.get_default_provider_config()
    provider_profile = (
        store.get_provider_profile(provider_config.provider_id) if provider_config else None
    )
    provider_verification = (
        store.get_provider_verification(provider_config.provider_id) if provider_config else None
    )
    agents = store.list_agent_records(limit=6)
    runs = store.list_run_records(limit=6)
    prompt_blocks = store.list_prompt_blocks(limit=6)
    security_events = store.list_security_events(limit=6)
    model_calls = store.list_model_calls(limit=6)
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>The Matrix Dashboard</title>
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
      --line: rgba(0, 255, 65, 0.12);
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
    /* Digital rain — the iconic falling code */
    #matrix-rain {{
      position: fixed;
      inset: 0;
      width: 100vw;
      height: 100vh;
      z-index: 0;
      opacity: 0.45;
      pointer-events: none;
    }}
    /* Subtle vignette — gives depth without hiding the rain */
    body::before {{
      content: '';
      position: fixed;
      inset: 0;
      background:
        radial-gradient(ellipse 95% 75% at 50% 50%,
          transparent 0%,
          rgba(0, 0, 0, 0.15) 60%,
          rgba(0, 0, 0, 0.45) 100%);
      pointer-events: none;
      z-index: 1;
    }}
    /* CRT scanlines — every other row of pixels */
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
    @keyframes blink {{
      0%, 49% {{ opacity: 1; }}
      50%, 100% {{ opacity: 0; }}
    }}
    @keyframes pulseDot {{
      0%, 100% {{ box-shadow: 0 0 4px var(--phosphor-bright), 0 0 8px rgba(0, 255, 65, 0.5); opacity: 1; }}
      50% {{ box-shadow: 0 0 2px var(--phosphor-bright); opacity: 0.45; }}
    }}
    @keyframes wake {{
      from {{ opacity: 0; transform: translateY(8px); filter: blur(4px); }}
      to {{ opacity: 1; transform: translateY(0); filter: blur(0); }}
    }}
    @keyframes bootLine {{
      from {{ opacity: 0; transform: translateX(-12px); }}
      to {{ opacity: 1; transform: translateX(0); }}
    }}
    @keyframes phosphorPulse {{
      0%, 100% {{ text-shadow: 0 0 4px rgba(0, 255, 65, 0.95), 0 0 18px rgba(0, 255, 65, 0.55), 0 0 36px rgba(0, 255, 65, 0.25); }}
      50% {{ text-shadow: 0 0 6px rgba(0, 255, 65, 1), 0 0 24px rgba(0, 255, 65, 0.7), 0 0 48px rgba(0, 255, 65, 0.35); }}
    }}
    main {{
      position: relative;
      z-index: 2;
      width: min(1240px, calc(100% - 48px));
      margin: 0 auto;
      padding: 36px 0 48px;
    }}
    /* BOOT LOG — terminal-style intro lines */
    .boot-log {{
      margin-bottom: 18px;
      font-size: 13px;
      color: var(--phosphor-title);
      letter-spacing: 0.5px;
      line-height: 1.9;
    }}
    .boot-log p {{
      margin: 0;
      opacity: 0;
      animation: bootLine 380ms ease-out both;
    }}
    .boot-log p::before {{ content: '> '; color: var(--phosphor); }}
    .boot-log p:nth-child(1) {{ animation-delay: 80ms; }}
    .boot-log p:nth-child(2) {{ animation-delay: 240ms; }}
    .boot-log p:nth-child(3) {{ animation-delay: 400ms; }}
    .boot-log p:nth-child(4) {{ animation-delay: 560ms; color: var(--phosphor); }}
    /* HEADER */
    header {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 24px;
      align-items: end;
      padding-bottom: 22px;
      margin-bottom: 28px;
      border-bottom: 1px solid var(--line);
      animation: wake 700ms ease-out 600ms both;
    }}
    h1 {{
      font-family: "VT323", "Cascadia Mono", monospace;
      font-size: clamp(64px, 9vw, 112px);
      line-height: 0.85;
      margin: 0 0 12px;
      letter-spacing: 6px;
      color: var(--phosphor-bright);
      text-transform: uppercase;
      animation: phosphorPulse 4.5s ease-in-out infinite;
    }}
    h1::before {{
      content: '> ';
      color: var(--phosphor-title);
      letter-spacing: 0;
    }}
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
    .hud-meta {{
      text-align: right;
      font-size: 12px;
      color: var(--phosphor-title);
      letter-spacing: 1.5px;
      text-transform: uppercase;
      line-height: 1.9;
    }}
    .hud-meta strong {{ color: var(--phosphor-bright); font-weight: normal; }}
    .hud-meta .frame {{
      display: inline-block;
      border: 1px solid var(--line);
      padding: 2px 8px;
      margin-bottom: 4px;
      color: var(--phosphor);
    }}
    /* GRID */
    .grid {{
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 16px;
      counter-reset: panel;
    }}
    /* PANELS — HUD framed, with section IDs and corner brackets */
    .panel {{
      position: relative;
      background: var(--panel-bg);
      border: 1px solid var(--panel-edge);
      border-left: 2px solid var(--phosphor-dim);
      padding: 32px 18px 18px;
      counter-increment: panel;
      animation: wake 600ms ease-out both;
      transition: border-left-color 200ms ease, background 200ms ease;
    }}
    .panel::before {{
      content: '§' counter(panel, decimal-leading-zero);
      position: absolute;
      top: 10px; left: 16px;
      font-size: 11px;
      letter-spacing: 2.5px;
      color: var(--phosphor-title);
    }}
    .panel::after {{
      content: '';
      position: absolute;
      top: 6px; right: 6px;
      width: 14px; height: 14px;
      border-top: 1px solid var(--phosphor-dim);
      border-right: 1px solid var(--phosphor-dim);
      transition: border-color 200ms ease;
    }}
    .panel:hover {{
      border-left-color: var(--phosphor-bright);
      background: rgba(0, 22, 8, 0.85);
    }}
    .panel:hover::after {{ border-color: var(--phosphor-bright); }}
    /* Stagger panel reveal — feels like a HUD initializing */
    .panel:nth-child(1) {{ animation-delay: 700ms; }}
    .panel:nth-child(2) {{ animation-delay: 760ms; }}
    .panel:nth-child(3) {{ animation-delay: 820ms; }}
    .panel:nth-child(4) {{ animation-delay: 880ms; }}
    .panel:nth-child(5) {{ animation-delay: 940ms; }}
    .panel:nth-child(6) {{ animation-delay: 1000ms; }}
    .panel:nth-child(7) {{ animation-delay: 1060ms; }}
    .panel:nth-child(8) {{ animation-delay: 1120ms; }}
    .panel:nth-child(9) {{ animation-delay: 1180ms; }}
    .panel:nth-child(10) {{ animation-delay: 1240ms; }}
    .panel:nth-child(11) {{ animation-delay: 1300ms; }}
    .span-3 {{ grid-column: span 3; }}
    .span-4 {{ grid-column: span 4; }}
    .span-6 {{ grid-column: span 6; }}
    .span-8 {{ grid-column: span 8; }}
    .span-12 {{ grid-column: span 12; }}
    /* HEADINGS */
    h1, h2, h3, p {{ margin: 0; }}
    h2 {{
      margin: 0 0 14px;
      font-size: 13px;
      font-weight: normal;
      text-transform: uppercase;
      letter-spacing: 2.5px;
      color: var(--phosphor-title);
      padding-bottom: 8px;
      border-bottom: 1px dashed var(--line);
    }}
    h2::before {{ content: '// '; color: var(--phosphor-title); }}
    h3 {{
      margin: 0 0 4px;
      font-size: 14px;
      font-weight: normal;
      color: var(--phosphor);
      letter-spacing: 0.3px;
    }}
    p {{ color: var(--phosphor); }}
    .muted {{ color: var(--phosphor-title); }}
    strong {{ color: var(--phosphor-bright); font-weight: normal; }}
    /* METRIC PANELS */
    .panel.metric-panel {{
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      min-height: 144px;
      padding: 28px 18px 18px;
    }}
    .panel.metric-panel > p.muted {{
      font-size: 12px;
      letter-spacing: 2.5px;
      text-transform: uppercase;
      color: var(--phosphor-title);
    }}
    .metric {{
      font-family: "VT323", "Cascadia Mono", monospace;
      font-size: 76px;
      line-height: 1;
      color: var(--phosphor-bright);
      letter-spacing: 3px;
      margin-top: 6px;
      text-shadow:
        0 0 6px rgba(0, 255, 65, 0.95),
        0 0 22px rgba(0, 255, 65, 0.45);
    }}
    .metric-bar {{
      margin-top: 12px;
      height: 2px;
      background: linear-gradient(to right,
        var(--phosphor-bright) 0%,
        var(--phosphor-dim) 60%,
        transparent 100%);
      opacity: 0.7;
    }}
    /* TAGS */
    .tag {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid var(--line);
      padding: 4px 11px;
      color: var(--phosphor-title);
      font-size: 11px;
      white-space: nowrap;
      text-transform: uppercase;
      letter-spacing: 1.5px;
    }}
    .tag::before {{ content: '['; }}
    .tag::after {{ content: ']'; }}
    .tag.ok {{ border-color: rgba(0, 255, 65, 0.4); color: var(--phosphor-bright); background: rgba(0, 255, 65, 0.06); }}
    .tag.warn {{ border-color: rgba(255, 185, 77, 0.4); color: var(--phosphor-amber); background: rgba(255, 185, 77, 0.05); }}
    .tag.risk {{ border-color: rgba(255, 0, 60, 0.4); color: var(--phosphor-red); background: rgba(255, 0, 60, 0.06); }}
    /* LISTS */
    .list {{ display: grid; gap: 12px; }}
    .item {{
      position: relative;
      border-top: 1px dashed var(--line);
      padding: 12px 0 0 16px;
    }}
    .item::before {{
      content: '▸';
      position: absolute;
      left: 0; top: 12px;
      color: var(--phosphor-title);
      font-size: 11px;
    }}
    .item:first-child {{ border-top: 0; padding-top: 0; }}
    .item:first-child::before {{ top: 0; }}
    .row {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; }}
    .row h3 {{ min-width: 0; }}
    .row .tag {{ flex: 0 0 auto; }}
    code {{
      font-family: "Share Tech Mono", "Cascadia Mono", monospace;
      font-size: 13px;
      color: var(--phosphor-bright);
      text-shadow: 0 0 6px rgba(0, 255, 65, 0.4);
      overflow-wrap: anywhere;
    }}
    /* SYSTEM PATHS — terminal-style key/value rows */
    .system-paths .path-row {{
      display: flex;
      gap: 16px;
      align-items: baseline;
      padding: 8px 0;
      border-bottom: 1px dashed var(--line);
    }}
    .system-paths .path-row:last-child {{ border-bottom: 0; }}
    .system-paths .path-key {{
      flex: 0 0 90px;
      font-size: 12px;
      letter-spacing: 2.5px;
      text-transform: uppercase;
      color: var(--phosphor-title);
    }}
    .system-paths .path-value {{
      flex: 1;
      font-size: 14px;
      color: var(--phosphor-bright);
      text-shadow: 0 0 6px rgba(0, 255, 65, 0.35);
      word-break: break-all;
    }}
    /* FOOTER STATUS */
    .footer-bar {{
      margin-top: 28px;
      padding: 14px 0 4px;
      border-top: 1px solid var(--line);
      display: flex;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
      font-size: 11px;
      color: var(--phosphor-dim);
      letter-spacing: 2px;
      text-transform: uppercase;
      opacity: 0;
      animation: wake 600ms ease-out 1500ms both;
    }}
    .footer-bar .right {{ color: var(--phosphor); }}
    @media (max-width: 960px) {{
      h1 {{ font-size: 56px; letter-spacing: 3px; }}
      main {{ width: min(1240px, calc(100% - 28px)); }}
      header {{ grid-template-columns: 1fr; }}
      .hud-meta {{ text-align: left; }}
      .span-3, .span-4, .span-6, .span-8 {{ grid-column: span 12; }}
    }}
  </style>
</head>
<body>
  <canvas id="matrix-rain" aria-hidden="true"></canvas>
  <main>
    <div class="boot-log" aria-hidden="true">
      <p>connecting to vault @ <code>{escape(str(paths.vault))}</code></p>
      <p>oracle interface :: ready</p>
      <p>nebuchadnezzar runtime :: armed</p>
      <p>signal locked. you are not alone.</p>
    </div>
    <header>
      <div>
        <h1>The Matrix</h1>
        <div class="hud-status">
          <span><span class="dot"></span>node<span class="v">::online</span></span>
          <span>vault<span class="v">::sealed</span></span>
          <span>oracle<span class="v">::ready</span></span>
          <span>neo<span class="v">::watchful</span></span>
        </div>
      </div>
      <div class="hud-meta">
        <div class="frame">local agent system</div>
        <div>build <strong>0x4a5e</strong></div>
        <div>gen <strong>{escape(generated_at)}</strong></div>
      </div>
    </header>
    <section class="grid">
      {metric_panel("Runs", counts["runs"])}
      {metric_panel("Agents", counts["agents"])}
      {metric_panel("Prompt Blocks", counts["prompt_blocks"])}
      {metric_panel("Security Events", counts["security_events"])}
      {provider_panel(provider_config, provider_profile, provider_verification)}
      {runs_panel(runs, store)}
      {agents_panel(agents)}
      {security_panel(security_events)}
      {prompt_blocks_panel(prompt_blocks)}
      {model_calls_panel(model_calls)}
      <section class="panel system-paths span-12">
        <h2>System Paths</h2>
        <div class="path-row">
          <span class="path-key">home</span>
          <span class="path-value">{escape(str(paths.home))}</span>
        </div>
        <div class="path-row">
          <span class="path-key">vault</span>
          <span class="path-value">{escape(str(paths.vault))}</span>
        </div>
      </section>
    </section>
    <div class="footer-bar">
      <span>// the matrix has you</span>
      <span class="right">// follow the white rabbit &nbsp;▌</span>
    </div>
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
      setInterval(draw, 55);
    }})();
  </script>
</body>
</html>
"""


def metric_panel(title: str, value: int) -> str:
    return f"""
      <section class="panel metric-panel span-3">
        <p class="muted">{escape(title)}</p>
        <div>
          <div class="metric">{value}</div>
          <div class="metric-bar"></div>
        </div>
      </section>
"""


def provider_panel(provider_config, provider_profile, provider_verification) -> str:
    if provider_config is None:
        return """
      <section class="panel span-4">
        <div class="row"><h2>Provider</h2><span class="tag warn">not configured</span></div>
        <p class="muted">Run <code>the-matrix setup</code> to connect a local or cloud model.</p>
      </section>
"""
    display = provider_profile.display_name if provider_profile else provider_config.provider_id
    if provider_config.auth_mode.value == "none":
        secret = "not required"
    else:
        secret = "configured" if provider_config.secret_ref else "missing"
    if provider_verification is None:
        verification_text = "not checked"
        verification_class = "warn"
    else:
        verification_text = (
            f"{'ok' if provider_verification.get('ok') else 'failed'} "
            f"at {provider_verification.get('checked_at')}"
        )
        verification_class = "ok" if provider_verification.get("ok") else "risk"
    return f"""
      <section class="panel span-4">
        <div class="row"><h2>Provider</h2><span class="tag {verification_class}">configured</span></div>
        <p><strong>{escape(display)}</strong></p>
        <p class="muted">Model: <code>{escape(provider_config.selected_model)}</code></p>
        <p class="muted">Auth: {escape(provider_config.auth_mode.value)} ({secret})</p>
        <p class="muted">Verification: {escape(verification_text)}</p>
      </section>
"""


def runs_panel(runs: list[dict], store: RuntimeStore) -> str:
    items = []
    for record in runs:
        result = store.get_run(record["run_id"])
        metadata = result.metadata if result else {}
        task_count = metadata.get("mission_task_count", "unknown")
        completed = metadata.get("mission_completed_count", "unknown")
        items.append(
            f"""
          <div class="item">
            <div class="row">
              <h3><code>{escape(record["run_id"])}</code></h3>
              <span class="tag">{completed}/{task_count} tasks</span>
            </div>
            <p class="muted">{escape(_clip(record["request"], 150))}</p>
          </div>
"""
        )
    return f"""
      <section class="panel span-8">
        <h2>Recent Missions</h2>
        <div class="list">{''.join(items) or empty_text("No missions recorded yet.")}</div>
      </section>
"""


def agents_panel(agents: list[dict]) -> str:
    items = [
        f"""
          <div class="item">
            <div class="row">
              <h3><code>{escape(agent["agent_id"])}</code></h3>
              <span class="tag">{escape(agent["agent_type"])}/{escape(agent["risk_level"])}</span>
            </div>
            <p class="muted">{escape(_clip(agent["purpose"], 130))}</p>
            <p class="muted">success={agent["success_count"]} failure={agent["failure_count"]}</p>
          </div>
"""
        for agent in agents
    ]
    return f"""
      <section class="panel span-6">
        <h2>Active Agents</h2>
        <div class="list">{''.join(items) or empty_text("No agents deployed yet.")}</div>
      </section>
"""


def security_panel(events: list[dict]) -> str:
    items = []
    for event in events:
        tag = "ok" if event["approved"] else "risk"
        issues = "; ".join(event["issues"]) if event["issues"] else "none"
        items.append(
            f"""
          <div class="item">
            <div class="row">
              <h3>Event {event["id"]}</h3>
              <span class="tag {tag}">{escape(event["risk_level"])}</span>
            </div>
            <p class="muted">approved={bool(event["approved"])} run={escape(str(event["run_id"]))}</p>
            <p class="muted">{escape(_clip(issues, 140))}</p>
          </div>
"""
        )
    return f"""
      <section class="panel span-6">
        <h2>Neo Security</h2>
        <div class="list">{''.join(items) or empty_text("No security events recorded yet.")}</div>
      </section>
"""


def prompt_blocks_panel(prompt_blocks: list[dict]) -> str:
    items = [
        f"""
          <div class="item">
            <div class="row">
              <h3>{escape(block["block_ref"])}</h3>
              <span class="tag">{escape(block["block_type"])}</span>
            </div>
            <p class="muted">hash=<code>{escape(block["content_hash"][:12])}</code></p>
          </div>
"""
        for block in prompt_blocks
    ]
    return f"""
      <section class="panel span-6">
        <h2>Prompt Cache</h2>
        <div class="list">{''.join(items) or empty_text("No prompt blocks recorded yet.")}</div>
      </section>
"""


def model_calls_panel(model_calls: list[dict]) -> str:
    items = [
        f"""
          <div class="item">
            <div class="row">
              <h3>{escape(call["provider_id"])}</h3>
              <span class="tag {'ok' if call["ok"] else 'risk'}">{'ok' if call["ok"] else 'error'}</span>
            </div>
            <p class="muted"><code>{escape(call["model"])}</code></p>
            <p class="muted">chars={call["request_chars"]}->{call["response_chars"]}</p>
          </div>
"""
        for call in model_calls
    ]
    return f"""
      <section class="panel span-6">
        <h2>Model Calls</h2>
        <div class="list">{''.join(items) or empty_text("No model calls recorded yet.")}</div>
      </section>
"""


def empty_text(text: str) -> str:
    return f'<p class="muted">{escape(text)}</p>'


def _clip(value: str, limit: int) -> str:
    compact = " ".join(str(value).split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3].rstrip()}..."
