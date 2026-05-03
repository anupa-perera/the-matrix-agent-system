from thematrix.memory.vault import MemoryVault
from thematrix.schemas import AgentSpec, MissionPlan, MissionTask, TaskStatus
from thematrix.tools import FileDecision, FileOperation, FileToolResult, ShellCommandResult, ShellDecision


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
            ),
            FileToolResult(
                operation=FileOperation.READ,
                path="notes.md",
                purpose="Read notes.",
                decision=FileDecision.ALLOW,
                reason="Safe read.",
                executed=True,
                content="hello",
            )
        ],
    )

    output_path = tmp_path / "vault" / "raw" / "tool_outputs" / "run-1.md"
    assert output_path.exists()
    output = output_path.read_text(encoding="utf-8")
    assert "python --version" in output
    assert "notes.md" in output


def test_vault_records_mission_plan(tmp_path) -> None:
    vault = MemoryVault(tmp_path / "vault")
    vault.initialize()
    spec = AgentSpec(
        agent_id="builder-test-agent",
        agent_type="builder",
        purpose="Build safely.",
    )
    plan = MissionPlan(
        mission_id="mission-1",
        tasks=[
            MissionTask(
                sequence=1,
                title="Build",
                description="Build safely.",
                agent_spec=spec,
                status=TaskStatus.COMPLETED,
                result_summary="done",
            )
        ],
    )

    vault.record_mission_plan(plan)

    workflow_path = tmp_path / "vault" / "wiki" / "workflows" / "mission-1.md"
    assert workflow_path.exists()
    assert "Task Ledger" in workflow_path.read_text(encoding="utf-8")
