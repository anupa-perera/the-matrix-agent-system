from __future__ import annotations

from collections.abc import Callable
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
        progress_callback: Callable[[str, str, dict[str, object]], None] | None = None,
    ):
        self.oracle = oracle
        self.architect = architect
        self.neo = neo
        self.vault = vault
        self.store = store
        self.agent_runner = agent_runner
        self.progress_callback = progress_callback

    def run(
        self,
        user_request: str,
        privacy_mode: PrivacyMode,
        provider_config: ProviderConfig | None = None,
    ) -> MatrixRunResult:
        self._emit_progress("oracle", "Understanding the request.")
        brief = self.oracle.assess(user_request)
        self._emit_progress("architect", "Planning the mission and selecting agents.")
        plan = self.architect.plan_mission(
            brief,
            privacy_mode=privacy_mode,
            provider_config=provider_config,
        )
        self._emit_progress(
            "mission_planned",
            f"Architect planned {len(plan.tasks)} sequential task(s).",
            mission_id=plan.mission_id,
            task_count=len(plan.tasks),
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
                "architect_decisions": self._architect_decision_metadata(plan),
            },
        )
        self._persist_plan_result(plan, result, execution_tool_results)
        self._emit_progress(
            "complete",
            "Mission result is ready.",
            run_id=result.run_id,
            completed_count=result.metadata["mission_completed_count"],
            task_count=result.metadata["mission_task_count"],
        )
        return result

    def continue_mission(
        self,
        run_id: str,
        privacy_mode: PrivacyMode,
        provider_config: ProviderConfig | None = None,
    ) -> MatrixRunResult:
        previous = self.store.get_run(run_id)
        if previous is None:
            raise ValueError(f"No mission found for run id: {run_id}")
        tasks = self.store.list_mission_tasks(run_id=run_id, limit=100)
        if not tasks:
            raise ValueError(f"No mission tasks found for run id: {run_id}")

        self._emit_progress(
            "mission_resumed",
            "Continuing the unfinished mission.",
            run_id=run_id,
            task_count=len(tasks),
        )
        plan = MissionPlan(mission_id=run_id, tasks=tasks)
        previous_statuses = {task.task_id: task.status for task in plan.tasks}
        for task in plan.tasks:
            if task.status != TaskStatus.COMPLETED:
                self._prepare_task_for_continue(task, privacy_mode, provider_config)
        spec = plan.tasks[0].agent_spec
        human_layer = self.oracle.shape_human_layer(previous.oracle_brief, spec)
        task_run = self._run_sequential_plan(
            plan,
            previous.oracle_brief,
            previous.request,
            provider_config=provider_config,
            resume=True,
        )
        preflight = task_run["preflight_report"] or previous.preflight_report
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
            run_id=run_id,
            created_at=previous.created_at,
            request=previous.request,
            oracle_brief=previous.oracle_brief,
            agent_spec=spec,
            human_layer=human_layer,
            preflight_report=preflight,
            output_report=output_report,
            response=response,
            metadata={
                **previous.metadata,
                "runtime": "nebuchadnezzar",
                "resumed": True,
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
                "architect_decisions": self._architect_decision_metadata(plan),
            },
        )
        self._persist_plan_result(
            plan,
            result,
            execution_tool_results,
            previous_statuses=previous_statuses,
        )
        self._emit_progress(
            "complete",
            "Mission result is ready.",
            run_id=result.run_id,
            completed_count=result.metadata["mission_completed_count"],
            task_count=result.metadata["mission_task_count"],
        )
        return result

    def run_agent(
        self,
        agent_id: str,
        user_request: str,
        privacy_mode: PrivacyMode,
        provider_config: ProviderConfig | None = None,
    ) -> MatrixRunResult:
        spec = self.store.get_agent(agent_id)
        if spec is None:
            raise ValueError(f"No agent found for id: {agent_id}")
        if not spec.enabled:
            raise ValueError(f"Agent is paused: {agent_id}")

        self._emit_progress(
            "agent_selected",
            f"Loaded reusable agent `{agent_id}`.",
            agent_id=agent_id,
        )
        self._emit_progress("oracle", "Understanding the request for this agent.")
        brief = self.oracle.assess(user_request)
        agent_spec = spec.model_copy(deep=True)
        agent_spec.privacy_mode = privacy_mode
        if provider_config is None:
            agent_spec.provider_id = "unconfigured"
            agent_spec.model_id = "unconfigured"
        else:
            agent_spec.provider_id = provider_config.provider_id
            agent_spec.model_id = provider_config.selected_model

        plan = MissionPlan(
            strategy="manual_agent",
            tasks=[
                MissionTask(
                    sequence=1,
                    title=f"Manual run: {agent_spec.agent_type}",
                    description=user_request,
                    agent_spec=agent_spec,
                    architect_decision=(
                        f"User selected reusable agent `{agent_spec.agent_id}` directly."
                    ),
                )
            ],
        )
        human_layer = self.oracle.shape_human_layer(brief, agent_spec)
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
            agent_spec=agent_spec,
            human_layer=human_layer,
            preflight_report=preflight,
            output_report=output_report,
            response=response,
            metadata={
                "runtime": "nebuchadnezzar",
                "manual_agent_run": True,
                "selected_agent_id": agent_spec.agent_id,
                "oracle_assessment_source": getattr(
                    self.oracle,
                    "last_assessment_source",
                    "unknown",
                ),
                "architect_design_source": "manual_agent_selection",
                "architect_plan_source": "manual_agent_selection",
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
                "architect_decisions": self._architect_decision_metadata(plan),
            },
        )
        self._persist_plan_result(plan, result, execution_tool_results)
        self._emit_progress(
            "complete",
            "Agent result is ready.",
            run_id=result.run_id,
            completed_count=result.metadata["mission_completed_count"],
            task_count=result.metadata["mission_task_count"],
        )
        return result

    def _run_sequential_plan(
        self,
        plan: MissionPlan,
        brief,
        user_request: str,
        provider_config: ProviderConfig | None,
        resume: bool = False,
    ) -> dict[str, object]:
        first_preflight = None
        execution_status = "skipped"
        execution_error = None
        tool_results = []
        previous_results = [
            f"{task.title}: {task.result_summary}"
            for task in sorted(plan.tasks, key=lambda item: item.sequence)
            if task.status == TaskStatus.COMPLETED and task.result_summary
        ]
        for task in plan.tasks:
            if resume and task.status == TaskStatus.COMPLETED:
                continue
            if resume and task.status not in {
                TaskStatus.PENDING,
                TaskStatus.RUNNING,
                TaskStatus.SKIPPED,
                TaskStatus.FAILED,
                TaskStatus.BLOCKED,
            }:
                continue
            task.status = TaskStatus.RUNNING
            task.updated_at = datetime.now(UTC)
            self._emit_progress(
                "neo_preflight",
                f"Neo is reviewing task {task.sequence}: {task.title}",
                mission_id=plan.mission_id,
                task_id=task.task_id,
                task_sequence=task.sequence,
                agent_id=task.agent_spec.agent_id,
                agent_type=task.agent_spec.agent_type,
            )
            preflight = self.neo.review_agent_spec(task.agent_spec)
            if first_preflight is None:
                first_preflight = preflight
            if not preflight.approved:
                task.status = TaskStatus.BLOCKED
                task.error = "; ".join(preflight.issues)
                task.result_summary = "Neo blocked this task before execution."
                task.updated_at = datetime.now(UTC)
                self._emit_progress(
                    "blocked",
                    f"Neo blocked task {task.sequence}: {task.title}",
                    mission_id=plan.mission_id,
                    task_id=task.task_id,
                    task_sequence=task.sequence,
                    agent_id=task.agent_spec.agent_id,
                    issues=preflight.issues,
                )
                break
            if task.agent_spec.provider_id == "unconfigured":
                task.status = TaskStatus.SKIPPED
                task.result_summary = "No model provider is configured for execution."
                task.updated_at = datetime.now(UTC)
                self._emit_progress(
                    "skipped",
                    f"Task {task.sequence} is waiting for a configured model provider.",
                    mission_id=plan.mission_id,
                    task_id=task.task_id,
                    task_sequence=task.sequence,
                    agent_id=task.agent_spec.agent_id,
                )
                continue
            if self.agent_runner is None:
                task.status = TaskStatus.SKIPPED
                task.result_summary = "Runtime execution is not attached."
                task.updated_at = datetime.now(UTC)
                self._emit_progress(
                    "skipped",
                    f"Task {task.sequence} cannot run because execution is not attached.",
                    mission_id=plan.mission_id,
                    task_id=task.task_id,
                    task_sequence=task.sequence,
                    agent_id=task.agent_spec.agent_id,
                )
                continue
            self._emit_progress(
                "task_running",
                f"Running task {task.sequence} with `{task.agent_spec.agent_id}`.",
                mission_id=plan.mission_id,
                task_id=task.task_id,
                task_sequence=task.sequence,
                agent_id=task.agent_spec.agent_id,
                agent_type=task.agent_spec.agent_type,
            )
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
            self._emit_progress(
                "task_completed" if execution.executed else "task_failed",
                f"Task {task.sequence} {task.status.value}: {task.title}",
                mission_id=plan.mission_id,
                task_id=task.task_id,
                task_sequence=task.sequence,
                agent_id=task.agent_spec.agent_id,
                tool_result_count=task.tool_result_count,
                error=task.error,
            )
            if not execution.executed:
                break
        return {
            "preflight_report": first_preflight,
            "execution_status": execution_status,
            "execution_error": execution_error,
            "tool_results": tool_results,
        }

    def _prepare_task_for_continue(
        self,
        task: MissionTask,
        privacy_mode: PrivacyMode,
        provider_config: ProviderConfig | None,
    ) -> None:
        task.agent_spec.privacy_mode = privacy_mode
        if provider_config is None:
            task.agent_spec.provider_id = "unconfigured"
            task.agent_spec.model_id = "unconfigured"
        else:
            task.agent_spec.provider_id = provider_config.provider_id
            task.agent_spec.model_id = provider_config.selected_model

    def _persist_plan_result(
        self,
        plan: MissionPlan,
        result: MatrixRunResult,
        execution_tool_results: list[object],
        previous_statuses: dict[str, TaskStatus] | None = None,
    ) -> None:
        self.store.record_run(result)
        for task in plan.tasks:
            self.store.record_mission_task(result.run_id, task)
            if self._should_record_task_outcome(task, previous_statuses):
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

    def _should_record_task_outcome(
        self,
        task: MissionTask,
        previous_statuses: dict[str, TaskStatus] | None,
    ) -> bool:
        terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED}
        if task.status not in terminal:
            return False
        if previous_statuses is None:
            return True
        previous = previous_statuses.get(task.task_id)
        if previous == TaskStatus.COMPLETED and task.status == TaskStatus.COMPLETED:
            return False
        return True

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

    def _architect_decision_metadata(self, plan: MissionPlan) -> list[dict[str, object]]:
        return [
            {
                "sequence": task.sequence,
                "title": task.title,
                "agent_id": task.agent_spec.agent_id,
                "agent_type": task.agent_spec.agent_type,
                "reused": bool(task.agent_spec.reuse_candidate_id),
                "reuse_candidate_id": task.agent_spec.reuse_candidate_id,
                "decision": task.architect_decision,
            }
            for task in sorted(plan.tasks, key=lambda item: item.sequence)
        ]

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
        if plan.strategy == "manual_agent" and plan.tasks:
            task = sorted(plan.tasks, key=lambda item: item.sequence)[0]
            lines = [
                f"Manual agent run via `{task.agent_spec.agent_id}`. {provider_text}",
                f"Oracle shaped the mission as {human_layer.temperament}.",
                f"Status: {task.status.value}.",
            ]
            if task.status == TaskStatus.COMPLETED and task.result_summary:
                lines.extend(["", "Agent result:", "", task.result_summary])
            elif provider_config is None:
                lines.append("Configure a provider to execute this agent.")
            elif task.result_summary:
                lines.extend(["", task.result_summary])
            return "\n".join(lines)

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

    def _emit_progress(self, stage: str, message: str, **details: object) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(stage, message, details)
        except Exception:
            return
