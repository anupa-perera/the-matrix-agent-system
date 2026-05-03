from thematrix.architect import Architect
from thematrix.memory import MemoryVault, RuntimeStore
from thematrix.neo import Neo
from thematrix.oracle import Oracle
from thematrix.runtime import Nebuchadnezzar
from thematrix.schemas import PrivacyMode


def test_runtime_records_run(tmp_path) -> None:
    vault = MemoryVault(tmp_path / "vault")
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    vault.initialize()
    store.initialize()

    runtime = Nebuchadnezzar(
        oracle=Oracle(),
        architect=Architect(store),
        neo=Neo(),
        vault=vault,
        store=store,
    )

    result = runtime.run("Build a coding helper agent", privacy_mode=PrivacyMode.ASK_EACH_TIME)

    assert result.agent_spec is not None
    assert result.agent_spec.agent_type == "builder"
    assert result.preflight_report is not None
    assert result.preflight_report.approved
    assert result.metadata["oracle_assessment_source"] == "heuristic"
    assert result.metadata["architect_design_source"] == "heuristic"
    assert result.metadata["agent_execution_status"] == "skipped"
    assert result.metadata["tool_result_count"] == 0
    assert result.metadata["agent_outcome_recorded"] is False
    assert result.metadata["mission_strategy"] == "sequential"
    assert result.metadata["mission_task_count"] == 2
    assert result.metadata["mission_completed_count"] == 0
    assert "No model provider is configured yet." in result.response
    assert "Configure a provider to execute spawned agents." in result.response
    assert "Architect planned 2 sequential task(s)" in result.response
    assert result.agent_spec is not None
    assert (
        tmp_path / "vault" / "wiki" / "agents" / f"{result.agent_spec.agent_id}.md"
    ).exists()
    assert (tmp_path / "vault" / "wiki" / "workflows" / f"{result.run_id}.md").exists()
    tasks = store.list_mission_tasks(result.run_id)
    assert len(tasks) == 2
    assert [task.status.value for task in tasks] == ["skipped", "skipped"]
    assert [task.agent_spec.agent_type for task in tasks] == ["builder", "sentinel"]
    assert (
        tmp_path / "vault" / "raw" / "neo_reviews" / f"{result.run_id}-preflight.md"
    ).exists()
    assert (tmp_path / "vault" / "raw" / "neo_reviews" / f"{result.run_id}-output.md").exists()
    assert (tmp_path / "vault" / "raw" / "runs" / f"{result.run_id}.json").exists()
