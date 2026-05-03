from thematrix.memory.vault import MemoryVault
from thematrix.tools import ShellCommandResult, ShellDecision


def test_vault_initializes_obsidian_structure(tmp_path) -> None:
    vault = MemoryVault(tmp_path / "vault")

    vault.initialize()

    assert (tmp_path / "vault" / "index.md").exists()
    assert (tmp_path / "vault" / "log.md").exists()
    assert (tmp_path / "vault" / "raw" / "runs").is_dir()
    assert (tmp_path / "vault" / "wiki" / "agents").is_dir()
    assert (tmp_path / "vault" / "schema" / "security_rules.md").exists()


def test_vault_records_tool_outputs(tmp_path) -> None:
    vault = MemoryVault(tmp_path / "vault")
    vault.initialize()

    vault.record_tool_outputs(
        "run-1",
        [
            ShellCommandResult(
                command="python --version",
                purpose="Check Python availability.",
                decision=ShellDecision.ALLOW,
                reason="Command is low-risk.",
                executed=True,
                exit_code=0,
                stdout="Python 3\n",
            )
        ],
    )

    output_path = tmp_path / "vault" / "raw" / "tool_outputs" / "run-1.md"
    assert output_path.exists()
    assert "python --version" in output_path.read_text(encoding="utf-8")
