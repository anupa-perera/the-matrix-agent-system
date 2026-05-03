from thematrix.memory.vault import MemoryVault


def test_vault_initializes_obsidian_structure(tmp_path) -> None:
    vault = MemoryVault(tmp_path / "vault")

    vault.initialize()

    assert (tmp_path / "vault" / "index.md").exists()
    assert (tmp_path / "vault" / "log.md").exists()
    assert (tmp_path / "vault" / "raw" / "runs").is_dir()
    assert (tmp_path / "vault" / "wiki" / "agents").is_dir()
    assert (tmp_path / "vault" / "schema" / "security_rules.md").exists()

