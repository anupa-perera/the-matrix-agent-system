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
from thematrix.schemas import PrivacyMode

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
    result = runtime.run(request, privacy_mode=privacy)
    typer.echo(Oracle().finalize(result))
    typer.echo(f"Run logged: {result.run_id}")


@providers_app.command("list")
def list_providers() -> None:
    """Show the built-in provider catalog."""
    for provider in provider_catalog():
        auth = ", ".join(mode.value for mode in provider.auth_modes)
        typer.echo(f"{provider.provider_id}: {provider.display_name} [{provider.kind.value}] auth={auth}")
        typer.echo(f"  {provider.setup_hint}")


@memory_app.command("path")
def memory_path() -> None:
    """Print the default Obsidian vault path."""
    typer.echo(MatrixPaths().vault)


if __name__ == "__main__":
    app()

