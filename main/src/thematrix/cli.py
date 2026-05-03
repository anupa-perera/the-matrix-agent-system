from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from thematrix.architect import Architect
from thematrix.config import MatrixPaths, ensure_runtime_dirs
from thematrix.memory import MemoryVault, RuntimeStore
from thematrix.neo import Neo
from thematrix.oracle import Oracle
from thematrix.providers import provider_catalog
from thematrix.runtime import Nebuchadnezzar
from thematrix.schemas import AuthMode, FileChangeConsent, PrivacyMode, ProviderConfig, ProviderProfile
from thematrix.security import Keymaker, SecretStoreError

app = typer.Typer(help="The Matrix Agent System CLI.")
providers_app = typer.Typer(help="Manage model providers.")
memory_app = typer.Typer(help="Inspect memory locations.")
app.add_typer(providers_app, name="providers")
app.add_typer(memory_app, name="memory")


def bootstrap(paths: MatrixPaths) -> tuple[MemoryVault, RuntimeStore]:
    ensure_runtime_dirs(paths)
    vault = MemoryVault(paths.vault)
    store = RuntimeStore(paths.runtime_db)
    vault.initialize()
    store.initialize()
    for provider in provider_catalog():
        store.upsert_provider(provider)
    return vault, store


@app.command()
def init(
    home: Annotated[Path | None, typer.Option(help="Override Matrix home path.")] = None,
    vault: Annotated[Path | None, typer.Option(help="Override Obsidian vault path.")] = None,
) -> None:
    """Create the global Matrix home and Obsidian vault."""
    paths = MatrixPaths(
        home=home or MatrixPaths().home,
        vault=vault or MatrixPaths().vault,
    )
    bootstrap(paths)
    typer.echo("The Matrix is initialized.")
    typer.echo(f"Home:  {paths.home}")
    typer.echo(f"Vault: {paths.vault}")
    typer.echo(f"DB:    {paths.runtime_db}")


@app.command()
def ask(
    request: Annotated[str, typer.Argument(help="User request to route through The Matrix.")],
    privacy: Annotated[
        PrivacyMode,
        typer.Option(help="Privacy mode for this request."),
    ] = PrivacyMode.ASK_EACH_TIME,
) -> None:
    """Run a request through Oracle, Architect, Neo, and the runtime."""
    paths = MatrixPaths()
    vault, store = bootstrap(paths)
    runtime = Nebuchadnezzar(
        oracle=Oracle(),
        architect=Architect(store),
        neo=Neo(),
        vault=vault,
        store=store,
    )
    result = runtime.run(
        request,
        privacy_mode=privacy,
        provider_config=store.get_default_provider_config(),
    )
    typer.echo(Oracle().finalize(result))
    typer.echo(f"Run logged: {result.run_id}")


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
    auth_mode = _resolve_auth_choice(profile)
    secret_ref = _resolve_secret_ref(profile, auth_mode)
    file_consent = _resolve_file_change_consent()

    config = ProviderConfig(
        provider_id=profile.provider_id,
        selected_model=selected_model,
        auth_mode=auth_mode,
        secret_ref=secret_ref,
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
    typer.echo(f"Provider: {display_name}")
    typer.echo(f"Model:    {config.selected_model}")
    typer.echo(f"Auth:     {config.auth_mode.value}")
    typer.echo(f"Secrets:  {'configured' if config.secret_ref else 'not required'}")
    typer.echo(f"Default:  {config.is_default}")
    typer.echo(f"Files:    {config.file_change_consent.value}")


@memory_app.command("path")
def memory_path() -> None:
    """Print the default Obsidian vault path."""
    typer.echo(MatrixPaths().vault)


def _resolve_provider_choice(store: RuntimeStore, provider_id: str | None) -> ProviderProfile:
    profiles = store.list_provider_profiles()
    if provider_id:
        profile = store.get_provider_profile(provider_id)
        if profile is None:
            valid = ", ".join(profile.provider_id for profile in profiles)
            raise typer.BadParameter(f"Unknown provider. Valid providers: {valid}")
        return profile

    typer.echo("Choose a model provider:")
    for index, profile in enumerate(profiles, start=1):
        typer.echo(f"{index}. {profile.display_name} ({profile.provider_id}) - {profile.kind.value}")

    choice = typer.prompt("Provider", default="1")
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


def _resolve_secret_ref(profile: ProviderProfile, auth_mode: AuthMode) -> str | None:
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
    raise typer.BadParameter(
        "No writable OS secret backend is available. "
        f"Install the secrets extra or set {env_name} in your environment."
    )


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
