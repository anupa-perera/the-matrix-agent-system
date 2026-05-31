from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
import webbrowser

import typer

from thematrix.architect import Architect
from thematrix.config import MatrixPaths, ensure_runtime_dirs
from thematrix.memory import MemoryVault, RuntimeStore
from thematrix.neo import Neo
from thematrix.oracle import Oracle
from thematrix.onboarding import OnboardingService
from thematrix.prompts import PromptLibrary
from thematrix.providers import (
    ModelGatewayError,
    default_model_gateway,
    detect_local_providers,
    provider_catalog,
)
from thematrix.runtime import AgentRunner, Nebuchadnezzar
from thematrix.schemas import (
    AgentSpec,
    AuthMode,
    FileChangeConsent,
    MatrixRunResult,
    ModelRequest,
    OnboardingProfile,
    PrivacyMode,
    ProviderConfig,
    ProviderProfile,
    ReasoningEffort,
)
from thematrix.security import Keymaker, SecretStoreError
from thematrix.tools import FileExecutor, ShellExecutor
from thematrix.ui import serve_app_ui, serve_setup_ui, write_dashboard

app = typer.Typer(help="The Matrix Agent System CLI.")
providers_app = typer.Typer(help="Manage model providers.")
memory_app = typer.Typer(help="Inspect memory locations.")
agents_app = typer.Typer(help="Inspect reusable agent specs.")
missions_app = typer.Typer(help="Inspect and continue mission ledgers.")
app.add_typer(providers_app, name="providers")
app.add_typer(memory_app, name="memory")
app.add_typer(agents_app, name="agents")
app.add_typer(missions_app, name="missions")


def bootstrap(paths: MatrixPaths) -> tuple[MemoryVault, RuntimeStore]:
    ensure_runtime_dirs(paths)
    vault = MemoryVault(paths.vault)
    store = RuntimeStore(paths.runtime_db)
    prompt_library = PromptLibrary(paths.prompts_dir)
    vault.initialize()
    store.initialize()
    prompt_library.install_defaults()
    store.record_prompt_block(
        block_ref="oracle-assess-v1",
        block_type="role_prompt",
        content=prompt_library.read("oracle_assess"),
    )
    store.record_prompt_block(
        block_ref="architect-design-v1",
        block_type="role_prompt",
        content=prompt_library.read("architect_design"),
    )
    store.record_prompt_block(
        block_ref="architect-plan-v1",
        block_type="role_prompt",
        content=prompt_library.read("architect_plan"),
    )
    for provider in provider_catalog():
        store.upsert_provider(provider)
    return vault, store


@app.command()
def init(
    home: Annotated[Path | None, typer.Option(help="Override Matrix home path.")] = None,
    vault: Annotated[Path | None, typer.Option(help="Override Obsidian vault path.")] = None,
    onboarding: Annotated[
        bool,
        typer.Option("--onboarding/--no-onboarding", help="Offer the first-run setup wizard."),
    ] = True,
) -> None:
    """Create the global Matrix home and Obsidian vault."""
    paths = MatrixPaths(
        home=home or MatrixPaths().home,
        vault=vault or MatrixPaths().vault,
    )
    memory_vault, store = bootstrap(paths)
    typer.echo("The Matrix is initialized.")
    typer.echo(f"Home:  {paths.home}")
    typer.echo(f"Vault: {paths.vault}")
    typer.echo(f"DB:    {paths.runtime_db}")
    if onboarding and not store.get_preference("onboarding_complete"):
        if typer.confirm("Run the first-run onboarding wizard now?", default=True):
            _run_onboarding_wizard(paths, memory_vault, store)


@app.command()
def setup() -> None:
    """Run the first-run onboarding wizard."""
    paths = MatrixPaths()
    vault, store = bootstrap(paths)
    _run_onboarding_wizard(paths, vault, store)


@app.command("setup-ui")
def setup_ui(
    port: Annotated[
        int,
        typer.Option(help="Localhost port. Use 0 to choose an available port."),
    ] = 0,
    open_browser: Annotated[
        bool,
        typer.Option("--open/--no-open", help="Open the setup page in the browser."),
    ] = True,
) -> None:
    """Run local-only browser onboarding."""
    paths = MatrixPaths()
    vault, store = bootstrap(paths)
    typer.echo("Starting local-only setup UI on 127.0.0.1.")
    typer.echo("A random URL token is required for every request.")
    try:
        url = serve_setup_ui(
            paths,
            vault,
            store,
            port=port,
            open_browser=open_browser,
            url_callback=lambda value: typer.echo(f"Setup UI: {value}"),
        )
    except KeyboardInterrupt:
        typer.echo("Setup UI stopped.")
        return
    typer.echo(f"Setup UI closed: {url}")


@app.command()
def start(
    open_browser: Annotated[
        bool,
        typer.Option("--open/--no-open", help="Open browser setup/app pages."),
    ] = True,
    test_provider: Annotated[
        bool,
        typer.Option("--test-provider/--no-test-provider", help="Run a readiness check if needed."),
    ] = True,
    setup_timeout: Annotated[
        int,
        typer.Option(help="Seconds before the setup UI closes automatically."),
    ] = 15 * 60,
    app_timeout: Annotated[
        int,
        typer.Option(help="Seconds before the local app UI closes automatically."),
    ] = 60 * 60,
) -> None:
    """Beginner-friendly setup and launch flow."""
    paths = MatrixPaths()
    vault, store = bootstrap(paths)

    typer.echo("Welcome to The Matrix.")
    typer.echo("I will check the local setup and open a guided setup page if anything is missing.")
    typer.echo(f"Home:  {paths.home}")
    typer.echo(f"Vault: {paths.vault}")

    provider_config = store.get_default_provider_config()
    onboarding_complete = store.get_preference("onboarding_complete") is True
    if not onboarding_complete or provider_config is None:
        typer.echo("Setup is not complete yet. Opening the guided local setup page.")
        typer.echo("The page runs only on 127.0.0.1 and uses a random URL token.")
        try:
            serve_setup_ui(
                paths,
                vault,
                store,
                open_browser=open_browser,
                url_callback=lambda value: typer.echo(f"Setup page: {value}"),
                timeout_seconds=setup_timeout,
            )
        except KeyboardInterrupt:
            typer.echo("Setup was stopped.")
            raise typer.Exit(code=1) from None
        provider_config = store.get_default_provider_config()
        onboarding_complete = store.get_preference("onboarding_complete") is True

    if not onboarding_complete or provider_config is None:
        typer.echo("Setup is still incomplete. Run `the-matrix start` again when ready.")
        raise typer.Exit(code=1)

    _maybe_check_provider(store, provider_config, test_provider)
    dashboard_path = write_dashboard(paths, store)
    typer.echo("The Matrix is ready.")
    typer.echo(f"Dashboard: {dashboard_path}")
    typer.echo("Opening the local app UI. Press Ctrl+C in this terminal to stop it.")
    try:
        url = serve_app_ui(
            paths,
            vault,
            store,
            request_runner=lambda request, progress_callback=None: _run_browser_request(
                paths,
                vault,
                store,
                request,
                progress_callback=progress_callback,
            ),
            agent_request_runner=lambda agent_id, request, progress_callback=None: _run_browser_agent_request(
                paths,
                vault,
                store,
                agent_id,
                request,
                progress_callback=progress_callback,
            ),
            open_browser=open_browser,
            url_callback=lambda value: typer.echo(f"App UI: {value}"),
            timeout_seconds=app_timeout,
        )
    except KeyboardInterrupt:
        typer.echo("App UI stopped.")
        return
    typer.echo(f"App UI closed: {url}")


@app.command()
def doctor() -> None:
    """Show local setup health without exposing secrets."""
    paths = MatrixPaths()
    _, store = bootstrap(paths)
    keymaker = Keymaker()
    provider_config = store.get_default_provider_config()

    typer.echo("The Matrix doctor")
    typer.echo(f"Home:  {_status(paths.home.exists())} {paths.home}")
    typer.echo(f"Vault: {_status(paths.vault.exists())} {paths.vault}")
    typer.echo(f"DB:    {_status(paths.runtime_db.exists())} {paths.runtime_db}")
    typer.echo(f"Prompts: {_status((paths.prompts_dir / 'oracle_assess.md').exists())} installed")
    typer.echo(f"Secrets backend: {keymaker.backend_name} writable={keymaker.can_write}")

    onboarding_complete = store.get_preference("onboarding_complete") is True
    typer.echo(f"Onboarding: {_status(onboarding_complete)}")

    if provider_config is None:
        typer.echo("Provider: missing - run `the-matrix setup` or `the-matrix providers configure`")
    else:
        profile = store.get_provider_profile(provider_config.provider_id)
        secret_status = "not required"
        if provider_config.auth_mode != AuthMode.NONE:
            secret_status = "configured" if provider_config.secret_ref else "missing"
        typer.echo(
            "Provider: "
            f"{_status(provider_config.enabled)} "
            f"{profile.display_name if profile else provider_config.provider_id}"
        )
        typer.echo(f"Model: {provider_config.selected_model}")
        typer.echo(f"Auth: {provider_config.auth_mode.value} secret={secret_status}")
        verification = store.get_provider_verification(provider_config.provider_id)
        if verification is None:
            typer.echo("Verification: not checked")
        else:
            typer.echo(
                f"Verification: {_status(bool(verification['ok']))} "
                f"{verification['checked_at']} {verification['message']}"
            )

    prompt_blocks = store.list_prompt_blocks(limit=5)
    agents = store.list_agent_records(limit=5)
    typer.echo(f"Prompt blocks indexed: {len(prompt_blocks)}")
    typer.echo(f"Reusable agents indexed: {len(agents)}")


@app.command()
def ask(
    request: Annotated[str, typer.Argument(help="User request to route through The Matrix.")],
    privacy: Annotated[
        PrivacyMode | None,
        typer.Option(help="Privacy mode for this request."),
    ] = None,
) -> None:
    """Run a request through Oracle, Architect, Neo, and the runtime."""
    paths = MatrixPaths()
    vault, store = bootstrap(paths)
    selected_privacy = privacy or _default_privacy_mode(store)
    provider_config = store.get_default_provider_config()
    oracle, runtime = _build_runtime(paths, vault, store, provider_config)
    result = runtime.run(
        request,
        privacy_mode=selected_privacy,
        provider_config=provider_config,
    )
    typer.echo(oracle.finalize(result))
    typer.echo(f"Run logged: {result.run_id}")


@app.command()
def ui(
    open_browser: Annotated[
        bool,
        typer.Option("--open/--no-open", help="Open the generated dashboard in the browser."),
    ] = False,
) -> None:
    """Generate a small local HTML dashboard."""
    paths = MatrixPaths()
    _, store = bootstrap(paths)
    dashboard_path = write_dashboard(paths, store)
    typer.echo(f"Dashboard written: {dashboard_path}")
    if open_browser:
        webbrowser.open(Path(dashboard_path).as_uri())


@missions_app.command("list")
def list_missions(
    limit: Annotated[int, typer.Option(help="Maximum number of missions to show.")] = 20,
) -> None:
    """List recent mission ledgers."""
    _, store = bootstrap(MatrixPaths())
    records = store.list_run_records(limit=limit)
    if not records:
        typer.echo("No missions are recorded yet.")
        return
    for record in records:
        tasks = store.list_mission_tasks(run_id=record["run_id"], limit=100)
        completed = sum(1 for task in tasks if task.status.value == "completed")
        reused = sum(1 for task in tasks if task.agent_spec.reuse_candidate_id)
        typer.echo(
            f"{record['run_id']} tasks={completed}/{len(tasks)} "
            f"reused={reused} created={record['created_at']}"
        )
        typer.echo(f"  request: {record['request'][:160]}")


@missions_app.command("show")
def show_mission(
    run_id: Annotated[str, typer.Argument(help="Mission run id.")],
) -> None:
    """Show one mission ledger."""
    _, store = bootstrap(MatrixPaths())
    result = store.get_run(run_id)
    if result is None:
        typer.echo(f"No mission found for run id: {run_id}")
        raise typer.Exit(code=1)
    typer.echo(f"Mission: {run_id}")
    typer.echo(f"Request: {result.request}")
    typer.echo(f"Response: {result.response[:240]}")
    tasks = store.list_mission_tasks(run_id=run_id, limit=100)
    if not tasks:
        typer.echo("No tasks are recorded for this mission.")
        return
    for task in tasks:
        typer.echo(
            f"{task.sequence}. {task.title} [{task.status.value}] "
            f"agent={task.agent_spec.agent_id}"
        )
        typer.echo(f"  reuse: {_agent_reuse_label(task.agent_spec)}")
        if task.architect_decision:
            typer.echo(f"  architect: {task.architect_decision}")
        if task.result_summary:
            typer.echo(f"  result: {task.result_summary[:180]}")


@missions_app.command("continue")
def continue_mission(
    run_id: Annotated[str, typer.Argument(help="Mission run id to continue.")],
    privacy: Annotated[
        PrivacyMode | None,
        typer.Option(help="Privacy mode for this continuation."),
    ] = None,
) -> None:
    """Continue a previous mission from unfinished tasks."""
    paths = MatrixPaths()
    vault, store = bootstrap(paths)
    selected_privacy = privacy or _default_privacy_mode(store)
    provider_config = store.get_default_provider_config()
    oracle, runtime = _build_runtime(paths, vault, store, provider_config)
    try:
        result = runtime.continue_mission(
            run_id,
            privacy_mode=selected_privacy,
            provider_config=provider_config,
        )
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    typer.echo(oracle.finalize(result))
    typer.echo(f"Mission updated: {result.run_id}")


def _build_runtime(
    paths: MatrixPaths,
    vault: MemoryVault,
    store: RuntimeStore,
    provider_config: ProviderConfig | None,
    approval_callback=None,
    progress_callback=None,
) -> tuple[Oracle, Nebuchadnezzar]:
    prompt_library = PromptLibrary(paths.prompts_dir)
    gateway = default_model_gateway(store)
    oracle = Oracle(
        model_gateway=gateway,
        prompt_library=prompt_library,
    )
    runtime = Nebuchadnezzar(
        oracle=oracle,
        architect=Architect(
            store,
            model_gateway=gateway,
            prompt_library=prompt_library,
        ),
        neo=Neo(),
        vault=vault,
        store=store,
        progress_callback=progress_callback,
        agent_runner=AgentRunner(
            gateway,
            prompt_library,
            shell_executor=ShellExecutor(
                approval_callback=approval_callback or _approve_shell_command,
                cwd=Path.cwd(),
            ),
            file_executor=FileExecutor(
                root=Path.cwd(),
                approval_callback=approval_callback or _approve_file_write,
                file_change_consent=(
                    provider_config.file_change_consent
                    if provider_config
                    else FileChangeConsent.ASK_EACH_TIME
                ),
            ),
        ),
    )
    return oracle, runtime


def _run_browser_request(
    paths: MatrixPaths,
    vault: MemoryVault,
    store: RuntimeStore,
    request: str,
    progress_callback=None,
) -> MatrixRunResult:
    provider_config = store.get_default_provider_config()
    selected_privacy = _default_privacy_mode(store)
    _, runtime = _build_runtime(
        paths,
        vault,
        store,
        provider_config,
        approval_callback=_deny_browser_approval,
        progress_callback=progress_callback,
    )
    return runtime.run(
        request,
        privacy_mode=selected_privacy,
        provider_config=provider_config,
    )


def _run_browser_agent_request(
    paths: MatrixPaths,
    vault: MemoryVault,
    store: RuntimeStore,
    agent_id: str,
    request: str,
    progress_callback=None,
) -> MatrixRunResult:
    provider_config = store.get_default_provider_config()
    selected_privacy = _default_privacy_mode(store)
    _, runtime = _build_runtime(
        paths,
        vault,
        store,
        provider_config,
        approval_callback=_deny_browser_approval,
        progress_callback=progress_callback,
    )
    return runtime.run_agent(
        agent_id,
        request,
        privacy_mode=selected_privacy,
        provider_config=provider_config,
    )


def _deny_browser_approval(target: str, reason: str, purpose: str) -> bool:
    return False


def _maybe_check_provider(
    store: RuntimeStore,
    provider_config: ProviderConfig,
    should_check: bool,
) -> None:
    verification = store.get_provider_verification(provider_config.provider_id)
    if verification and verification.get("ok"):
        typer.echo(f"Provider already verified: {provider_config.provider_id}")
        return
    if not should_check:
        typer.echo("Provider readiness was not checked.")
        return

    typer.echo(f"Checking provider readiness: {provider_config.provider_id}")
    health = default_model_gateway(store).health_check(provider_config)
    store.record_provider_verification(
        provider_id=provider_config.provider_id,
        ok=health.ok,
        message=health.message,
        model=provider_config.selected_model,
    )
    if health.ok:
        typer.echo(f"Provider ready: {health.message}")
    else:
        typer.echo(f"Provider check failed: {health.message}")
        typer.echo("You can still use planning mode, or run `the-matrix setup-ui` to adjust settings.")


@agents_app.command("list")
def list_agents(
    limit: Annotated[int, typer.Option(help="Maximum number of agents to show.")] = 20,
) -> None:
    """Show reusable agents tracked by Architect."""
    _, store = bootstrap(MatrixPaths())
    records = store.list_agent_records(limit=limit)
    if not records:
        typer.echo("No reusable agents are recorded yet.")
        return
    for record in records:
        typer.echo(
            f"{record['agent_id']} [{record['agent_type']}/{record['risk_level']}] "
            f"{record['purpose']}"
        )
        typer.echo(
            f"  last_used={record['last_used_at']} "
            f"success={record['success_count']} failure={record['failure_count']}"
        )


@agents_app.command("show")
def show_agent(
    agent_id: Annotated[str, typer.Argument(help="Agent id to inspect.")],
) -> None:
    """Show one reusable agent spec without reading prompt text."""
    _, store = bootstrap(MatrixPaths())
    spec = store.get_agent(agent_id)
    if spec is None:
        typer.echo(f"No agent found for id: {agent_id}")
        raise typer.Exit(code=1)
    typer.echo(f"Agent:   {spec.agent_id}")
    typer.echo(f"Type:    {spec.agent_type}")
    typer.echo(f"Purpose: {spec.purpose}")
    typer.echo(f"Risk:    {spec.risk_level.value}")
    typer.echo(f"Reuse:   {spec.reusable}")
    typer.echo(f"Provider:{spec.provider_id}")
    typer.echo(f"Model:   {spec.model_id}")
    typer.echo("Tools:")
    for tool in spec.tools_allowed:
        typer.echo(f"  - {tool}")
    typer.echo("Memory:")
    for scope in spec.memory_scope:
        typer.echo(f"  - {scope}")
    typer.echo("Prompt blocks:")
    for block_ref in spec.prompt_block_refs:
        typer.echo(f"  - {block_ref}")


@providers_app.command("list")
def list_providers() -> None:
    """Show the built-in provider catalog."""
    for provider in provider_catalog():
        auth = ", ".join(mode.value for mode in provider.auth_modes)
        typer.echo(f"{provider.provider_id}: {provider.display_name} [{provider.kind.value}] auth={auth}")
        if provider.suggested_models:
            typer.echo(f"  suggested models: {', '.join(provider.suggested_models)}")
        typer.echo(f"  {provider.setup_hint}")


@providers_app.command("configure")
def configure_provider(
    provider_id: Annotated[
        str | None,
        typer.Argument(help="Provider id. Omit for the interactive wizard."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(help="Model id. Omit to choose interactively."),
    ] = None,
    reasoning_effort: Annotated[
        str | None,
        typer.Option(help="Reasoning effort for providers that support it."),
    ] = None,
    base_url: Annotated[
        str | None,
        typer.Option(help="Provider base URL. Mostly useful for custom endpoints."),
    ] = None,
    make_default: Annotated[
        bool,
        typer.Option("--default/--no-default", help="Make this the default provider."),
    ] = True,
) -> None:
    """Configure a provider through an interactive wizard."""
    paths = MatrixPaths()
    vault, store = bootstrap(paths)
    profile = _resolve_provider_choice(store, provider_id)
    selected_model = _resolve_model_choice(profile, model)
    selected_reasoning_effort = _resolve_reasoning_effort(profile, reasoning_effort)
    selected_base_url = _resolve_base_url(profile, base_url)
    auth_mode = _resolve_auth_choice(profile)
    secret_ref = _resolve_secret_ref(profile, auth_mode)
    file_consent = _resolve_file_change_consent()

    config = ProviderConfig(
        provider_id=profile.provider_id,
        selected_model=selected_model,
        auth_mode=auth_mode,
        secret_ref=secret_ref,
        base_url=selected_base_url,
        reasoning_effort=selected_reasoning_effort,
        is_default=make_default,
        file_change_consent=file_consent,
    )
    store.configure_provider(config)
    store.set_preference("default_provider_id", profile.provider_id)
    vault.append_log(
        title="Provider configured",
        body=(
            f"Provider: {profile.display_name}\n\n"
            f"Model: {selected_model}\n\n"
            f"Reasoning effort: {selected_reasoning_effort or 'provider default'}\n\n"
            f"Base URL: {selected_base_url or profile.default_base_url or 'not configured'}\n\n"
            f"Auth mode: {auth_mode.value}\n\n"
            f"File-change consent: {file_consent.value}\n\n"
            "No secret values were written to the vault."
        ),
    )
    typer.echo(f"Configured {profile.display_name} with model {selected_model}.")
    if make_default:
        typer.echo("This provider is now the default.")


@providers_app.command("current")
def current_provider() -> None:
    """Show the current default provider without exposing secrets."""
    _, store = bootstrap(MatrixPaths())
    config = store.get_default_provider_config()
    if config is None:
        typer.echo("No provider is configured yet. Run: the-matrix providers configure")
        return

    profile = store.get_provider_profile(config.provider_id)
    display_name = profile.display_name if profile else config.provider_id
    secret_status = "not required"
    if config.auth_mode != AuthMode.NONE:
        secret_status = "configured" if config.secret_ref else "missing"
    typer.echo(f"Provider: {display_name}")
    typer.echo(f"Model:    {config.selected_model}")
    typer.echo(f"Reason:   {config.reasoning_effort or 'provider default'}")
    typer.echo(f"Base URL: {config.base_url or (profile.default_base_url if profile else 'unknown')}")
    typer.echo(f"Auth:     {config.auth_mode.value}")
    typer.echo(f"Secrets:  {secret_status}")
    typer.echo(f"Default:  {config.is_default}")
    typer.echo(f"Files:    {config.file_change_consent.value}")
    verification = store.get_provider_verification(config.provider_id)
    if verification is None:
        typer.echo("Verified: not checked")
    else:
        typer.echo(f"Verified: {bool(verification['ok'])} at {verification['checked_at']}")
        typer.echo(f"Message:  {verification['message']}")


@providers_app.command("detect")
def detect_providers() -> None:
    """Detect local model providers on this PC."""
    detections = detect_local_providers()
    for detection in detections:
        status = "reachable" if detection.reachable else "not reachable"
        typer.echo(f"{detection.provider_id}: {status} at {detection.base_url}")
        typer.echo(f"  {detection.message}")
        if detection.models:
            typer.echo(f"  models: {', '.join(detection.models[:8])}")


@providers_app.command("test")
def test_provider(
    provider_id: Annotated[
        str | None,
        typer.Argument(help="Provider id to test. Omit to test the default provider."),
    ] = None,
    prompt: Annotated[
        str,
        typer.Option(help="Small prompt used for the provider readiness check."),
    ] = "Reply with one short sentence saying the provider is ready.",
) -> None:
    """Send a small test request through the model gateway."""
    paths = MatrixPaths()
    vault, store = bootstrap(paths)
    config = store.get_provider_config(provider_id) if provider_id else store.get_default_provider_config()
    if config is None:
        typer.echo("No provider is configured. Run: the-matrix setup")
        raise typer.Exit(code=1)

    gateway = default_model_gateway(store)
    try:
        response = gateway.generate(ModelRequest.from_prompt(prompt), config=config)
    except ModelGatewayError as exc:
        store.record_provider_verification(
            provider_id=config.provider_id,
            ok=False,
            message=str(exc),
            model=config.selected_model,
        )
        vault.append_log(
            title="Provider test failed",
            body=(
                f"Provider: {config.provider_id}\n\n"
                f"Model: {config.selected_model}\n\n"
                f"Error type: {type(exc).__name__}\n\n"
                "No prompt text or secret values were written to the vault."
            ),
        )
        typer.echo(f"Provider test failed: {exc}")
        raise typer.Exit(code=1) from exc

    vault.append_log(
        title="Provider test completed",
        body=(
            f"Provider: {response.provider_id}\n\n"
            f"Model: {response.model}\n\n"
            f"Response characters: {len(response.text)}\n\n"
            "No prompt text or secret values were written to the vault."
        ),
    )
    typer.echo(f"Provider: {response.provider_id}")
    typer.echo(f"Model:    {response.model}")
    typer.echo(f"Ready:    {response.text}")
    store.record_provider_verification(
        provider_id=config.provider_id,
        ok=True,
        message=response.text,
        model=response.model,
    )


@memory_app.command("path")
def memory_path() -> None:
    """Print the default Obsidian vault path."""
    typer.echo(MatrixPaths().vault)


@memory_app.command("summary")
def memory_summary() -> None:
    """Show a compact memory dashboard in the terminal."""
    _, store = bootstrap(MatrixPaths())
    counts = store.overview_counts()
    provider_config = store.get_default_provider_config()
    typer.echo("The Matrix memory summary")
    if provider_config is None:
        typer.echo("Provider: not configured")
    else:
        typer.echo(
            f"Provider: {provider_config.provider_id} model={provider_config.selected_model}"
        )
    typer.echo(
        "Counts: "
        f"runs={counts['runs']} agents={counts['agents']} "
        f"prompt_blocks={counts['prompt_blocks']} security_events={counts['security_events']}"
    )
    recent_runs = store.list_run_records(limit=3)
    typer.echo("Recent runs:")
    if not recent_runs:
        typer.echo("  none")
    for record in recent_runs:
        typer.echo(f"  {record['run_id']} - {record['request'][:120]}")
    recent_events = store.list_security_events(limit=3)
    typer.echo("Recent security:")
    if not recent_events:
        typer.echo("  none")
    for event in recent_events:
        issues = "; ".join(event["issues"]) if event["issues"] else "none"
        typer.echo(
            f"  {event['id']} risk={event['risk_level']} "
            f"approved={bool(event['approved'])} issues={issues[:120]}"
        )


@memory_app.command("prompt-blocks")
def prompt_blocks(
    limit: Annotated[int, typer.Option(help="Maximum number of prompt blocks to show.")] = 20,
) -> None:
    """Show prompt-cache metadata without printing prompt text."""
    _, store = bootstrap(MatrixPaths())
    records = store.list_prompt_blocks(limit=limit)
    if not records:
        typer.echo("No prompt blocks are recorded yet.")
        return
    for record in records:
        typer.echo(
            f"{record['block_ref']} [{record['block_type']}] "
            f"hash={record['content_hash'][:12]} updated={record['updated_at']}"
        )


@memory_app.command("security")
def security_events(
    limit: Annotated[int, typer.Option(help="Maximum number of security events to show.")] = 20,
) -> None:
    """Show recent Neo security events."""
    _, store = bootstrap(MatrixPaths())
    records = store.list_security_events(limit=limit)
    if not records:
        typer.echo("No security events are recorded yet.")
        return
    for record in records:
        issues = "; ".join(record["issues"]) if record["issues"] else "none"
        typer.echo(
            f"{record['id']} run={record['run_id']} approved={bool(record['approved'])} "
            f"risk={record['risk_level']} issues={issues}"
        )


@memory_app.command("model-calls")
def model_calls(
    limit: Annotated[int, typer.Option(help="Maximum number of model calls to show.")] = 20,
) -> None:
    """Show model-call metadata without prompt or response text."""
    _, store = bootstrap(MatrixPaths())
    records = store.list_model_calls(limit=limit)
    if not records:
        typer.echo("No model calls are recorded yet.")
        return
    for record in records:
        status = "ok" if record["ok"] else f"error:{record['error_type']}"
        typer.echo(
            f"{record['id']} {record['created_at']} {record['provider_id']}/{record['model']} "
            f"{status} latency_ms={record['latency_ms']} "
            f"chars={record['request_chars']}->{record['response_chars']}"
        )


@memory_app.command("runs")
def memory_runs(
    run_id: Annotated[str | None, typer.Argument(help="Optional run id to inspect.")] = None,
    limit: Annotated[int, typer.Option(help="Maximum number of runs to show.")] = 20,
) -> None:
    """Show run metadata without opening raw JSON."""
    _, store = bootstrap(MatrixPaths())
    if run_id:
        result = store.get_run(run_id)
        if result is None:
            typer.echo(f"No run found for id: {run_id}")
            raise typer.Exit(code=1)
        _show_run_metadata(result)
        return

    records = store.list_run_records(limit=limit)
    if not records:
        typer.echo("No runs are recorded yet.")
        return
    for record in records:
        result = store.get_run(record["run_id"])
        task_count = result.metadata.get("mission_task_count", "unknown") if result else "unknown"
        completed = (
            result.metadata.get("mission_completed_count", "unknown") if result else "unknown"
        )
        runtime = result.metadata.get("runtime", "unknown") if result else "unknown"
        typer.echo(
            f"{record['run_id']} runtime={runtime} tasks={completed}/{task_count} "
            f"created={record['created_at']}"
        )
        typer.echo(f"  request: {record['request'][:160]}")


@memory_app.command("synthesize")
def memory_synthesize(
    limit: Annotated[int, typer.Option(help="Maximum number of recent runs to synthesize.")] = 10,
) -> None:
    """Write a deterministic Obsidian summary from recent runs."""
    paths = MatrixPaths()
    vault, store = bootstrap(paths)
    records = store.list_run_records(limit=limit)
    if not records:
        typer.echo("No runs are recorded yet.")
        return
    now = datetime.now(UTC)
    title = f"run-summary-{now.strftime('%Y%m%d-%H%M%S')}"
    lines = [
        f"# Run Summary {now.isoformat(timespec='seconds')}",
        "",
        "## Recent Runs",
        "",
    ]
    for record in records:
        result = store.get_run(record["run_id"])
        metadata = result.metadata if result else {}
        lines.extend(
            [
                f"### {record['run_id']}",
                "",
                f"- Request: {record['request']}",
                f"- Runtime: {metadata.get('runtime', 'unknown')}",
                f"- Tasks: {metadata.get('mission_completed_count', 'unknown')}/"
                f"{metadata.get('mission_task_count', 'unknown')}",
                f"- Execution: {metadata.get('agent_execution_status', 'unknown')}",
                "",
            ]
        )
        decisions = metadata.get("architect_decisions") or []
        if decisions:
            lines.append("Architect decisions:")
            lines.append("")
            for decision in decisions:
                reused = "reused" if decision.get("reused") else "spawned"
                lines.append(
                    f"- {decision.get('sequence')}. {reused} "
                    f"{decision.get('agent_id')}: {decision.get('decision')}"
                )
            lines.append("")
    lines.extend(
        [
            "## Candidate Wiki Updates",
            "",
            "- Review repeated requests for reusable workflow notes.",
            "- Review repeated blocked or failed tasks for risk notes.",
            "- Promote stable agent lessons into `wiki/agents/` when they recur.",
            "",
        ]
    )
    path = vault.record_synthesis(title, "\n".join(lines))
    typer.echo(f"Synthesized memory note: {path}")


@memory_app.command("tasks")
def mission_tasks(
    run_id: Annotated[str | None, typer.Option(help="Optional run id to filter tasks.")] = None,
    limit: Annotated[int, typer.Option(help="Maximum number of tasks to show.")] = 20,
) -> None:
    """Show recent sequential mission tasks."""
    _, store = bootstrap(MatrixPaths())
    records = store.list_mission_tasks(run_id=run_id, limit=limit)
    if not records:
        typer.echo("No mission tasks are recorded yet.")
        return
    for task in records:
        typer.echo(
            f"{task.sequence}. {task.title} [{task.status.value}] "
            f"agent={task.agent_spec.agent_id}"
        )
        typer.echo(f"  reuse: {_agent_reuse_label(task.agent_spec)}")
        if task.architect_decision:
            typer.echo(f"  architect: {task.architect_decision}")
        if task.result_summary:
            typer.echo(f"  result: {task.result_summary[:160]}")


def _agent_reuse_label(spec: AgentSpec) -> str:
    if spec.reuse_candidate_id:
        return f"reused:{spec.reuse_candidate_id}"
    return "spawned"


def _show_run_metadata(result: MatrixRunResult) -> None:
    metadata = result.metadata
    typer.echo(f"Run:      {result.run_id}")
    typer.echo(f"Created:  {result.created_at.isoformat()}")
    typer.echo(f"Runtime:  {metadata.get('runtime', 'unknown')}")
    typer.echo(f"Request:  {result.request}")
    typer.echo(f"Response: {result.response[:240]}")
    typer.echo(
        "Mission:  "
        f"strategy={metadata.get('mission_strategy', 'unknown')} "
        f"tasks={metadata.get('mission_completed_count', 'unknown')}/"
        f"{metadata.get('mission_task_count', 'unknown')}"
    )
    typer.echo(
        "Sources:  "
        f"oracle={metadata.get('oracle_assessment_source', 'unknown')} "
        f"architect_design={metadata.get('architect_design_source', 'unknown')} "
        f"architect_plan={metadata.get('architect_plan_source', 'unknown')}"
    )
    typer.echo(
        "Execution:"
        f" status={metadata.get('agent_execution_status', 'unknown')} "
        f"tool_results={metadata.get('tool_result_count', 'unknown')} "
        f"error={metadata.get('agent_execution_error') or 'none'}"
    )
    decisions = metadata.get("architect_decisions") or []
    if not decisions:
        typer.echo("Architect decisions: none recorded")
        return
    typer.echo("Architect decisions:")
    for decision in decisions:
        reused = "reused" if decision.get("reused") else "spawned"
        typer.echo(
            f"  {decision.get('sequence')}. {reused} "
            f"{decision.get('agent_id')} [{decision.get('agent_type')}]"
        )
        typer.echo(f"     {decision.get('decision') or 'No decision note recorded.'}")


def _run_onboarding_wizard(paths: MatrixPaths, vault: MemoryVault, store: RuntimeStore) -> None:
    typer.echo("Welcome to The Matrix.")
    typer.echo("This setup collects the minimum needed to run agents safely.")
    typer.echo(f"Home:  {paths.home}")
    typer.echo(f"Vault: {paths.vault}")

    profile = _resolve_provider_choice(store, None)
    selected_model = _resolve_model_choice(profile, None)
    selected_base_url = _resolve_base_url(profile, None)
    auth_mode = _resolve_auth_choice(profile)
    secret_ref = _resolve_secret_ref(profile, auth_mode, allow_skip=True)
    privacy_mode = _resolve_privacy_mode()
    file_consent = _resolve_file_change_consent()
    guarded_shell_enabled = typer.confirm(
        "Enable guarded shell access for agents when Neo approves it?",
        default=True,
    )

    provider_config = ProviderConfig(
        provider_id=profile.provider_id,
        selected_model=selected_model,
        auth_mode=auth_mode,
        secret_ref=secret_ref,
        base_url=selected_base_url,
        is_default=True,
        file_change_consent=file_consent,
    )
    onboarding_profile = OnboardingProfile(
        default_provider_id=profile.provider_id,
        default_model=selected_model,
        auth_mode=auth_mode,
        base_url=selected_base_url,
        privacy_mode=privacy_mode,
        file_change_consent=file_consent,
        guarded_shell_enabled=guarded_shell_enabled,
        vault_path=str(paths.vault),
        secret_configured=secret_ref is not None,
    )
    OnboardingService(store, vault).complete(onboarding_profile, provider_config)
    typer.echo("Onboarding is complete.")
    typer.echo("Your choices were saved. Secret values were not written to logs or memory.")


def _resolve_provider_choice(store: RuntimeStore, provider_id: str | None) -> ProviderProfile:
    profiles = provider_catalog()
    if provider_id:
        profile = store.get_provider_profile(provider_id)
        if profile is None:
            valid = ", ".join(profile.provider_id for profile in profiles)
            raise typer.BadParameter(f"Unknown provider. Valid providers: {valid}")
        return profile

    typer.echo("Choose a model provider:")
    for index, profile in enumerate(profiles, start=1):
        typer.echo(f"{index}. {profile.display_name} ({profile.provider_id}) - {profile.kind.value}")

    choice = typer.prompt("Provider")
    if choice.isdigit():
        selected_index = int(choice)
        if 1 <= selected_index <= len(profiles):
            return profiles[selected_index - 1]
    profile = store.get_provider_profile(choice)
    if profile:
        return profile
    raise typer.BadParameter("Provider choice was not recognized.")


def _resolve_model_choice(profile: ProviderProfile, model: str | None) -> str:
    if model:
        return model
    if profile.suggested_models:
        typer.echo("Suggested models:")
        for index, suggested in enumerate(profile.suggested_models, start=1):
            typer.echo(f"{index}. {suggested}")
    default_model = profile.suggested_models[0] if profile.suggested_models else "default"
    choice = typer.prompt("Model", default=default_model)
    if choice.isdigit() and profile.suggested_models:
        selected_index = int(choice)
        if 1 <= selected_index <= len(profile.suggested_models):
            return profile.suggested_models[selected_index - 1]
    return choice


def _resolve_reasoning_effort(
    profile: ProviderProfile,
    reasoning_effort: str | None,
) -> ReasoningEffort | None:
    if not reasoning_effort:
        return None
    supported = {effort.value for effort in profile.supported_reasoning_efforts}
    if not supported:
        raise typer.BadParameter(
            f"{profile.display_name} does not support a reasoning effort setting."
        )
    if reasoning_effort not in supported:
        valid = ", ".join(sorted(supported))
        raise typer.BadParameter(f"Reasoning effort must be one of: {valid}.")
    return ReasoningEffort(reasoning_effort)


def _resolve_base_url(profile: ProviderProfile, base_url: str | None) -> str | None:
    if base_url:
        return base_url
    if profile.provider_id == "custom-openai-compatible":
        return typer.prompt("Base URL", default="http://localhost:8000/v1")
    return profile.default_base_url


def _resolve_auth_choice(profile: ProviderProfile) -> AuthMode:
    if len(profile.auth_modes) == 1 and profile.auth_modes[0] in {
        AuthMode.NONE,
        AuthMode.EXTERNAL,
    }:
        return profile.auth_modes[0]

    supported_modes = [
        mode
        for mode in profile.auth_modes
        if mode in {AuthMode.API_KEY, AuthMode.LOCAL_TOKEN, AuthMode.NONE, AuthMode.EXTERNAL}
    ]
    if not supported_modes:
        raise typer.BadParameter("This provider does not have a supported v1 auth mode yet.")

    typer.echo("Choose auth mode:")
    for index, mode in enumerate(supported_modes, start=1):
        typer.echo(f"{index}. {mode.value}")
    choice = typer.prompt("Auth mode", default=supported_modes[0].value)
    if choice.isdigit():
        selected_index = int(choice)
        if 1 <= selected_index <= len(supported_modes):
            return supported_modes[selected_index - 1]
    for mode in supported_modes:
        if choice == mode.value:
            return mode
    raise typer.BadParameter("Auth mode choice was not recognized.")


def _resolve_secret_ref(
    profile: ProviderProfile,
    auth_mode: AuthMode,
    allow_skip: bool = False,
) -> str | None:
    if auth_mode in {AuthMode.NONE, AuthMode.EXTERNAL}:
        return None

    keymaker = Keymaker()
    if keymaker.can_write:
        secret_value = typer.prompt(
            f"{profile.display_name} credential",
            hide_input=True,
            confirmation_prompt=True,
        )
        try:
            return keymaker.store_api_key(profile.provider_id, secret_value).secret_ref
        except SecretStoreError as exc:
            raise typer.BadParameter(str(exc)) from exc

    env_ref = keymaker.api_key_ref(profile.provider_id)
    if keymaker.resolve_api_key(env_ref):
        typer.echo(f"Using API key from {Keymaker.env_var_name(profile.provider_id)}.")
        return env_ref

    env_name = Keymaker.env_var_name(profile.provider_id)
    if allow_skip:
        typer.echo(
            "No writable OS secret backend or environment variable was found. "
            f"You can set {env_name} later or reinstall with keyring support."
        )
        if typer.confirm("Continue setup without storing this credential?", default=True):
            return None
    raise typer.BadParameter(
        "No writable OS secret backend is available. "
        f"Install keyring support or set {env_name} in your environment."
    )


def _resolve_privacy_mode() -> PrivacyMode:
    modes = list(PrivacyMode)
    typer.echo("Choose default privacy mode:")
    for index, mode in enumerate(modes, start=1):
        typer.echo(f"{index}. {mode.value}")
    choice = typer.prompt("Privacy mode", default=PrivacyMode.ASK_EACH_TIME.value)
    if choice.isdigit():
        selected_index = int(choice)
        if 1 <= selected_index <= len(modes):
            return modes[selected_index - 1]
    for mode in modes:
        if choice == mode.value:
            return mode
    raise typer.BadParameter("Privacy mode choice was not recognized.")


def _default_privacy_mode(store: RuntimeStore) -> PrivacyMode:
    value = store.get_preference("default_privacy_mode")
    if isinstance(value, str):
        try:
            return PrivacyMode(value)
        except ValueError:
            return PrivacyMode.ASK_EACH_TIME
    return PrivacyMode.ASK_EACH_TIME


def _resolve_file_change_consent() -> FileChangeConsent:
    allow = typer.confirm(
        "Allow this provider to make file changes without asking each time?",
        default=False,
    )
    if allow:
        return FileChangeConsent.ALLOW_ALWAYS
    return FileChangeConsent.ASK_EACH_TIME


def _status(ok: bool) -> str:
    return "ok" if ok else "missing"


def _approve_shell_command(command: str, reason: str, purpose: str) -> bool:
    typer.echo("A spawned agent requested a shell command that needs approval.")
    typer.echo(f"Purpose: {purpose or 'not provided'}")
    typer.echo(f"Reason:  {reason}")
    typer.echo(f"Command: {command}")
    return typer.confirm("Run this command?", default=False)


def _approve_file_write(path: str, reason: str, purpose: str) -> bool:
    typer.echo("A spawned agent requested a file write that needs approval.")
    typer.echo(f"Purpose: {purpose or 'not provided'}")
    typer.echo(f"Reason:  {reason}")
    typer.echo(f"Path:    {path}")
    return typer.confirm("Write this file?", default=False)


if __name__ == "__main__":
    app()
