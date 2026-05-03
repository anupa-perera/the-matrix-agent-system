from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from thematrix.architect import Architect
from thematrix.config import MatrixPaths, ensure_runtime_dirs
from thematrix.memory import MemoryVault, RuntimeStore
from thematrix.neo import Neo
from thematrix.oracle import Oracle
from thematrix.onboarding import OnboardingService
from thematrix.prompts import PromptLibrary
from thematrix.providers import ModelGatewayError, default_model_gateway, provider_catalog
from thematrix.runtime import AgentRunner, Nebuchadnezzar
from thematrix.schemas import (
    AuthMode,
    FileChangeConsent,
    ModelRequest,
    OnboardingProfile,
    PrivacyMode,
    ProviderConfig,
    ProviderProfile,
)
from thematrix.security import Keymaker, SecretStoreError

app = typer.Typer(help="The Matrix Agent System CLI.")
providers_app = typer.Typer(help="Manage model providers.")
memory_app = typer.Typer(help="Inspect memory locations.")
agents_app = typer.Typer(help="Inspect reusable agent specs.")
app.add_typer(providers_app, name="providers")
app.add_typer(memory_app, name="memory")
app.add_typer(agents_app, name="agents")


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
        agent_runner=AgentRunner(gateway, prompt_library),
    )
    result = runtime.run(
        request,
        privacy_mode=selected_privacy,
        provider_config=store.get_default_provider_config(),
    )
    typer.echo(oracle.finalize(result))
    typer.echo(f"Run logged: {result.run_id}")


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
    typer.echo(f"Base URL: {config.base_url or (profile.default_base_url if profile else 'unknown')}")
    typer.echo(f"Auth:     {config.auth_mode.value}")
    typer.echo(f"Secrets:  {secret_status}")
    typer.echo(f"Default:  {config.is_default}")
    typer.echo(f"Files:    {config.file_change_consent.value}")


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


@memory_app.command("path")
def memory_path() -> None:
    """Print the default Obsidian vault path."""
    typer.echo(MatrixPaths().vault)


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


def _resolve_base_url(profile: ProviderProfile, base_url: str | None) -> str | None:
    if base_url:
        return base_url
    if profile.provider_id == "custom-openai-compatible":
        return typer.prompt("Base URL", default="http://localhost:8000/v1")
    return profile.default_base_url


def _resolve_auth_choice(profile: ProviderProfile) -> AuthMode:
    if profile.auth_modes == [AuthMode.NONE]:
        return AuthMode.NONE

    supported_modes = [
        mode
        for mode in profile.auth_modes
        if mode in {AuthMode.API_KEY, AuthMode.LOCAL_TOKEN, AuthMode.NONE}
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
    if auth_mode == AuthMode.NONE:
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


if __name__ == "__main__":
    app()
