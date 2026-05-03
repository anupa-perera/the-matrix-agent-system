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
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f8f5;
      --panel: #ffffff;
      --text: #1f2523;
      --muted: #65706b;
      --line: #dce1db;
      --green: #1f7a4d;
      --cyan: #0d6b72;
      --red: #b74343;
      --gold: #8a651c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 "Segoe UI", system-ui, -apple-system, sans-serif;
    }}
    main {{
      width: min(1180px, calc(100% - 32px));
      margin: 0 auto;
      padding: 28px 0 42px;
    }}
    header {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 18px;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--line);
    }}
    h1, h2, h3, p {{ margin: 0; }}
    h1 {{ font-size: 30px; font-weight: 700; letter-spacing: 0; }}
    h2 {{ font-size: 17px; margin-bottom: 12px; }}
    h3 {{ font-size: 14px; margin-bottom: 4px; }}
    .muted {{ color: var(--muted); }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 14px;
      margin-top: 18px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }}
    .span-3 {{ grid-column: span 3; }}
    .span-4 {{ grid-column: span 4; }}
    .span-6 {{ grid-column: span 6; }}
    .span-8 {{ grid-column: span 8; }}
    .span-12 {{ grid-column: span 12; }}
    .metric {{ font-size: 30px; font-weight: 700; }}
    .tag {{
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 2px 8px;
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }}
    .tag.ok {{ border-color: #bad9c8; color: var(--green); }}
    .tag.warn {{ border-color: #ead8a8; color: var(--gold); }}
    .tag.risk {{ border-color: #e6c0bd; color: var(--red); }}
    .list {{ display: grid; gap: 10px; }}
    .item {{ border-top: 1px solid var(--line); padding-top: 10px; }}
    .item:first-child {{ border-top: 0; padding-top: 0; }}
    .row {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; }}
    code {{
      overflow-wrap: anywhere;
      font-family: "Cascadia Mono", Consolas, monospace;
      font-size: 12px;
      color: var(--cyan);
    }}
    @media (max-width: 860px) {{
      header {{ align-items: flex-start; flex-direction: column; }}
      .span-3, .span-4, .span-6, .span-8 {{ grid-column: span 12; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>The Matrix</h1>
        <p class="muted">Local agent system dashboard</p>
      </div>
      <div class="muted">Generated {escape(generated_at)}</div>
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
      <section class="panel span-12">
        <h2>Memory Paths</h2>
        <p><span class="muted">Home:</span> <code>{escape(str(paths.home))}</code></p>
        <p><span class="muted">Vault:</span> <code>{escape(str(paths.vault))}</code></p>
      </section>
    </section>
  </main>
</body>
</html>
"""


def metric_panel(title: str, value: int) -> str:
    return f"""
      <section class="panel span-3">
        <p class="muted">{escape(title)}</p>
        <div class="metric">{value}</div>
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
        <h2>Recent Runs</h2>
        <div class="list">{''.join(items) or empty_text("No runs recorded yet.")}</div>
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
        <h2>Reusable Agents</h2>
        <div class="list">{''.join(items) or empty_text("No reusable agents recorded yet.")}</div>
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
