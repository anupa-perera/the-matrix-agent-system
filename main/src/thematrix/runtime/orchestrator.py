from __future__ import annotations

from datetime import UTC, datetime

from thematrix.architect import Architect
from thematrix.memory import MemoryVault, RuntimeStore
from thematrix.neo import Neo
from thematrix.oracle import Oracle
from thematrix.runtime.agent_runner import AgentRunner
from thematrix.schemas import (
    MatrixRunResult,
    MissionPlan,
    MissionTask,
    PrivacyMode,
    ProviderConfig,
    TaskStatus,
)


class Nebuchadnezzar:
    """Runtime mission flow for one user request."""

    def __init__(
        self,
        oracle: Oracle,
        architect: Architect,
        neo: Neo,
        vault: MemoryVault,
        store: RuntimeStore,
        agent_runner: AgentRunner | None = None,
    ):
        self.oracle = oracle
        self.architect = architect
        self.neo = neo
        self.vault = vault
        self.store = store
        self.agent_runner = agent_runner

    def run(
        self,
        user_request: str,
        privacy_mode: PrivacyMode,
        provider_config: ProviderConfig | None = None,
    ) -> MatrixRunResult:
        brief = self.oracle.assess(user_request)
        plan = self.architect.plan_mission(
            brief,
            privacy_mode=privacy_mode,
            provider_config=provider_config,
        )
        primary_task = plan.tasks[0]
        spec = primary_task.agent_spec
        human_layer = self.oracle.shape_human_layer(brief, spec)
        task_run = self._run_sequential_plan(
            plan,
            brief,
            user_request,
            provider_config=provider_config,
        )
        preflight = task_run["preflight_report"]
        execution_status = task_run["execution_status"]
        execution_error = task_run["execution_error"]
        execution_tool_results = task_run["tool_results"]
        response = self._render_response(plan, human_layer, provider_config)
        output_report = self.neo.review_output(response)
        agent_outcome_success = self._agent_outcome_success(
            preflight_approved=preflight.approved if preflight else False,
            output_approved=output_report.approved,
            execution_status=execution_status,
        )
        result = MatrixRunResult(
            run_id=plan.mission_id,
            request=user_request,
            oracle_brief=brief,
            agent_spec=spec,
            human_layer=human_layer,
            preflight_report=preflight,
            output_report=output_report,
            response=response,
            metadata={
                "runtime": "nebuchadnezzar",
                "oracle_assessment_source": getattr(
                    self.oracle,
                    "last_assessment_source",
                    "unknown",
                ),
                "architect_design_source": getattr(
                    self.architect,
                    "last_design_source",
                    "unknown",
                ),
                "architect_plan_source": getattr(
                    self.architect,
                    "last_plan_source",
                    "unknown",
                ),
                "agent_execution_status": execution_status,
                "agent_execution_error": execution_error,
                "tool_result_count": len(execution_tool_results),
                "agent_outcome_recorded": agent_outcome_success is not None,
                "agent_outcome_success": agent_outcome_success,
                "mission_strategy": plan.strategy,
                "mission_task_count": len(plan.tasks),
                "mission_completed_count": sum(
                    1 for task in plan.tasks if task.status == TaskStatus.COMPLETED
                ),
            },
        )
        self.store.record_run(result)
        for task in plan.tasks:
            self.store.record_mission_task(result.run_id, task)
            if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED}:
                self.store.record_agent_outcome(
                    task.agent_spec.agent_id,
                    success=task.status == TaskStatus.COMPLETED,
                )
        for task in plan.tasks:
            self.vault.record_agent_spec(task.agent_spec)
        self.vault.record_mission_plan(plan)
        self.vault.record_tool_outputs(result.run_id, execution_tool_results)
        if result.preflight_report is not None:
            self.vault.record_security_review(result.run_id, "preflight", result.preflight_report)
        if result.output_report is not None:
            self.vault.record_security_review(result.run_id, "output", result.output_report)
        self.vault.record_run(result)
        return result

    def _run_sequential_plan(
        self,
        plan: MissionPlan,
        brief,
        user_request: str,
        provider_config: ProviderConfig | None,
    ) -> dict[str, object]:
        first_preflight = None
        execution_status = "skipped"
        execution_error = None
        tool_results = []
        previous_results: list[str] = []
        for task in plan.tasks:
            task.status = TaskStatus.RUNNING
            task.updated_at = datetime.now(UTC)
            preflight = self.neo.review_agent_spec(task.agent_spec)
            if first_preflight is None:
                first_preflight = preflight
            if not preflight.approved:
                task.status = TaskStatus.BLOCKED
                task.error = "; ".join(preflight.issues)
                task.result_summary = "Neo blocked this task before execution."
                task.updated_at = datetime.now(UTC)
                break
            if task.agent_spec.provider_id == "unconfigured":
                task.status = TaskStatus.SKIPPED
                task.result_summary = "No model provider is configured for execution."
                task.updated_at = datetime.now(UTC)
                continue
            if self.agent_runner is None:
                task.status = TaskStatus.SKIPPED
                task.result_summary = "Runtime execution is not attached."
                task.updated_at = datetime.now(UTC)
                continue
            execution = self.agent_runner.run(
                task.agent_spec,
                brief,
                self._task_request(user_request, task, previous_results),
                provider_config=provider_config,
            )
            execution_status = "executed" if execution.executed else "error"
            execution_error = execution.error
            task.tool_result_count = len(execution.tool_results or [])
            tool_results.extend(execution.tool_results or [])
            task.result_summary = execution.response
            task.status = TaskStatus.COMPLETED if execution.executed else TaskStatus.FAILED
            task.error = execution.error
            task.updated_at = datetime.now(UTC)
            previous_results.append(f"{task.title}: {execution.response}")
            if not execution.executed:
                break
        return {
            "preflight_report": first_preflight,
            "execution_status": execution_status,
            "execution_error": execution_error,
            "tool_results": tool_results,
        }

    def _task_request(
        self,
        user_request: str,
        task: MissionTask,
        previous_results: list[str],
    ) -> str:
        context = "\n\n".join(previous_results) if previous_results else "No previous task results."
        return (
            f"Original user request:\n{user_request}\n\n"
            f"Current sequential task:\n{task.description}\n\n"
            f"Previous task results:\n{context}"
        )

    def _render_response(
        self,
        plan: MissionPlan,
        human_layer,
        provider_config: ProviderConfig | None,
    ) -> str:
        provider_text = (
            "No model provider is configured yet."
            if provider_config is None
            else f"Provider `{provider_config.provider_id}` with model `{provider_config.selected_model}` is configured."
        )
        lines = [
            f"Architect planned {len(plan.tasks)} sequential task(s). {provider_text}",
            f"Oracle shaped the mission as {human_layer.temperament}.",
        ]
        for task in sorted(plan.tasks, key=lambda item: item.sequence):
            lines.append(
                f"{task.sequence}. {task.title} - {task.status.value} "
                f"via `{task.agent_spec.agent_id}`"
            )
        completed = [task for task in plan.tasks if task.status == TaskStatus.COMPLETED]
        if completed:
            lines.extend(["", "Final sequential result:", "", completed[-1].result_summary])
        elif provider_config is None:
            lines.append("Configure a provider to execute spawned agents.")
        return "\n".join(lines)

    def _agent_outcome_success(
        self,
        preflight_approved: bool,
        output_approved: bool,
        execution_status: str,
    ) -> bool | None:
        if not preflight_approved:
            return False
        if execution_status == "skipped":
            return None
        if execution_status == "error":
            return False
        return output_approved
