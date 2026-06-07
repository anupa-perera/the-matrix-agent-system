from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from thematrix.schemas import (
    AgentSpec,
    MatrixRunResult,
    MissionTask,
    OperatorGoal,
    OperatorGoalRun,
    OperatorGoalStatus,
    ProviderConfig,
    ProviderProfile,
)


class RuntimeStore:
    """SQLite-backed runtime index for exact lookup and audit metadata."""

    SQLITE_BUSY_TIMEOUT_MS = 5000

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=self.SQLITE_BUSY_TIMEOUT_MS / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(f"PRAGMA busy_timeout={self.SQLITE_BUSY_TIMEOUT_MS}")
        return connection

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    agent_type TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    last_used_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_agents_type_purpose
                ON agents(agent_type, purpose);

                CREATE TABLE IF NOT EXISTS prompt_blocks (
                    block_ref TEXT PRIMARY KEY,
                    block_type TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS providers (
                    provider_id TEXT PRIMARY KEY,
                    profile_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    selected_model TEXT
                );

                CREATE TABLE IF NOT EXISTS provider_settings (
                    provider_id TEXT PRIMARY KEY,
                    selected_model TEXT NOT NULL,
                    auth_mode TEXT NOT NULL,
                    secret_ref TEXT,
                    base_url TEXT,
                    reasoning_effort TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    is_default INTEGER NOT NULL DEFAULT 1,
                    file_change_consent TEXT NOT NULL,
                    configured_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS model_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    latency_ms INTEGER,
                    request_chars INTEGER NOT NULL,
                    response_chars INTEGER NOT NULL,
                    error_type TEXT
                );

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    request TEXT NOT NULL,
                    response TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS mission_tasks (
                    task_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    task_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_mission_tasks_run_sequence
                ON mission_tasks(run_id, sequence);

                CREATE TABLE IF NOT EXISTS operator_goals (
                    goal_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    next_run_at TEXT,
                    last_run_at TEXT,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    goal_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_operator_goals_status_next_run
                ON operator_goals(status, next_run_at);

                CREATE TABLE IF NOT EXISTS operator_goal_runs (
                    run_id TEXT PRIMARY KEY,
                    goal_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    run_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_operator_goal_runs_goal_created
                ON operator_goal_runs(goal_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS security_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    created_at TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    approved INTEGER NOT NULL,
                    issues_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS preferences (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(conn, "provider_settings", "base_url", "TEXT")
            self._ensure_column(conn, "provider_settings", "reasoning_effort", "TEXT")

    def upsert_agent(self, spec: AgentSpec) -> None:
        payload = spec.model_dump_json()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO agents(agent_id, agent_type, purpose, risk_level, spec_json, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    agent_type = excluded.agent_type,
                    purpose = excluded.purpose,
                    risk_level = excluded.risk_level,
                    spec_json = excluded.spec_json,
                    last_used_at = excluded.last_used_at
                """,
                (
                    spec.agent_id,
                    spec.agent_type,
                    spec.purpose,
                    spec.risk_level.value,
                    payload,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def find_reusable_agent(self, agent_type: str, purpose: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT agent_id FROM agents
                WHERE agent_type = ? AND purpose = ?
                ORDER BY success_count DESC, last_used_at DESC
                LIMIT 1
                """,
                (agent_type, purpose),
            ).fetchone()
        return str(row["agent_id"]) if row else None

    def list_reusable_agents(self, agent_type: str, limit: int = 12) -> list[AgentSpec]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT spec_json FROM agents
                WHERE agent_type = ?
                ORDER BY success_count DESC, failure_count ASC, last_used_at DESC
                LIMIT ?
                """,
                (agent_type, limit),
            ).fetchall()
        specs = [AgentSpec.model_validate_json(row["spec_json"]) for row in rows]
        return [spec for spec in specs if spec.reusable and spec.enabled]

    def list_agent_records(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    agent_id,
                    agent_type,
                    purpose,
                    risk_level,
                    success_count,
                    failure_count,
                    last_used_at,
                    spec_json
                FROM agents
                ORDER BY last_used_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            spec = AgentSpec.model_validate_json(record.pop("spec_json"))
            record["reusable"] = spec.reusable
            record["enabled"] = spec.enabled
            records.append(record)
        return records

    def overview_counts(self) -> dict[str, int]:
        tables = [
            "agents",
            "prompt_blocks",
            "runs",
            "mission_tasks",
            "security_events",
            "model_calls",
            "operator_goals",
        ]
        with self.connect() as conn:
            return {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in tables
            }

    def list_run_records(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, created_at, request, response
                FROM runs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_run(self, run_id: str) -> MatrixRunResult | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT result_json FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return MatrixRunResult.model_validate_json(row["result_json"])

    def get_agent(self, agent_id: str) -> AgentSpec | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT spec_json FROM agents WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
        if row is None:
            return None
        return AgentSpec.model_validate_json(row["spec_json"])

    def touch_agent(self, agent_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE agents
                SET last_used_at = ?
                WHERE agent_id = ?
                """,
                (datetime.now(UTC).isoformat(), agent_id),
            )

    def record_agent_outcome(self, agent_id: str, success: bool) -> None:
        column = "success_count" if success else "failure_count"
        with self.connect() as conn:
            conn.execute(
                f"""
                UPDATE agents
                SET {column} = {column} + 1,
                    last_used_at = ?
                WHERE agent_id = ?
                """,
                (datetime.now(UTC).isoformat(), agent_id),
            )

    def record_mission_task(self, run_id: str, task: MissionTask) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO mission_tasks(
                    task_id,
                    run_id,
                    sequence,
                    title,
                    status,
                    agent_id,
                    task_json,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    sequence = excluded.sequence,
                    title = excluded.title,
                    status = excluded.status,
                    agent_id = excluded.agent_id,
                    task_json = excluded.task_json,
                    updated_at = excluded.updated_at
                """,
                (
                    task.task_id,
                    run_id,
                    task.sequence,
                    task.title,
                    task.status.value,
                    task.agent_spec.agent_id,
                    task.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def list_mission_tasks(self, run_id: str | None = None, limit: int = 20) -> list[MissionTask]:
        if run_id is None:
            query = """
                SELECT task_json FROM mission_tasks
                ORDER BY updated_at DESC, sequence ASC
                LIMIT ?
            """
            params: tuple[object, ...] = (limit,)
        else:
            query = """
                SELECT task_json FROM mission_tasks
                WHERE run_id = ?
                ORDER BY sequence ASC
                LIMIT ?
            """
            params = (run_id, limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [MissionTask.model_validate_json(row["task_json"]) for row in rows]

    def upsert_operator_goal(self, goal: OperatorGoal) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO operator_goals(
                    goal_id,
                    status,
                    kind,
                    title,
                    next_run_at,
                    last_run_at,
                    failure_count,
                    goal_json,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(goal_id) DO UPDATE SET
                    status = excluded.status,
                    kind = excluded.kind,
                    title = excluded.title,
                    next_run_at = excluded.next_run_at,
                    last_run_at = excluded.last_run_at,
                    failure_count = excluded.failure_count,
                    goal_json = excluded.goal_json,
                    updated_at = excluded.updated_at
                """,
                (
                    goal.goal_id,
                    goal.status.value,
                    goal.kind.value,
                    goal.title,
                    goal.next_run_at.isoformat() if goal.next_run_at else None,
                    goal.last_run_at.isoformat() if goal.last_run_at else None,
                    goal.failure_count,
                    goal.model_dump_json(),
                    goal.updated_at.isoformat(),
                ),
            )

    def get_operator_goal(self, goal_id: str) -> OperatorGoal | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT goal_json FROM operator_goals WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()
        return OperatorGoal.model_validate_json(row["goal_json"]) if row else None

    def list_operator_goals(
        self,
        status: OperatorGoalStatus | None = None,
        limit: int = 20,
    ) -> list[OperatorGoal]:
        if status is None:
            query = """
                SELECT goal_json FROM operator_goals
                ORDER BY updated_at DESC
                LIMIT ?
            """
            params: tuple[object, ...] = (limit,)
        else:
            query = """
                SELECT goal_json FROM operator_goals
                WHERE status = ?
                ORDER BY next_run_at IS NULL, next_run_at ASC, updated_at DESC
                LIMIT ?
            """
            params = (status.value, limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [OperatorGoal.model_validate_json(row["goal_json"]) for row in rows]

    def list_due_operator_goals(self, now: datetime, limit: int = 20) -> list[OperatorGoal]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT goal_json FROM operator_goals
                WHERE status = ? AND next_run_at IS NOT NULL AND next_run_at <= ?
                ORDER BY next_run_at ASC
                LIMIT ?
                """,
                (OperatorGoalStatus.ACTIVE.value, now.isoformat(), limit),
            ).fetchall()
        return [OperatorGoal.model_validate_json(row["goal_json"]) for row in rows]

    def record_operator_goal_run(self, run: OperatorGoalRun) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO operator_goal_runs(
                    run_id,
                    goal_id,
                    created_at,
                    status,
                    message,
                    run_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.goal_id,
                    run.created_at.isoformat(),
                    run.status.value,
                    run.message,
                    run.model_dump_json(),
                ),
            )

    def list_operator_goal_runs(
        self,
        goal_id: str | None = None,
        limit: int = 20,
    ) -> list[OperatorGoalRun]:
        if goal_id is None:
            query = """
                SELECT run_json FROM operator_goal_runs
                ORDER BY created_at DESC
                LIMIT ?
            """
            params: tuple[object, ...] = (limit,)
        else:
            query = """
                SELECT run_json FROM operator_goal_runs
                WHERE goal_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            """
            params = (goal_id, limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [OperatorGoalRun.model_validate_json(row["run_json"]) for row in rows]

    def record_prompt_block(self, block_ref: str, block_type: str, content: str) -> None:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO prompt_blocks(block_ref, block_type, content_hash, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(block_ref) DO UPDATE SET
                    block_type = excluded.block_type,
                    content_hash = excluded.content_hash,
                    updated_at = excluded.updated_at
                """,
                (block_ref, block_type, digest, datetime.now(UTC).isoformat()),
            )

    def list_prompt_blocks(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT block_ref, block_type, content_hash, updated_at
                FROM prompt_blocks
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_provider(self, profile: ProviderProfile) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO providers(provider_id, profile_json)
                VALUES (?, ?)
                ON CONFLICT(provider_id) DO UPDATE SET profile_json = excluded.profile_json
                """,
                (profile.provider_id, profile.model_dump_json()),
            )

    def list_provider_profiles(self) -> list[ProviderProfile]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT profile_json FROM providers ORDER BY provider_id"
            ).fetchall()
        return [ProviderProfile.model_validate_json(row["profile_json"]) for row in rows]

    def get_provider_profile(self, provider_id: str) -> ProviderProfile | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT profile_json FROM providers WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
        return ProviderProfile.model_validate_json(row["profile_json"]) if row else None

    def configure_provider(self, config: ProviderConfig) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if config.is_default:
                conn.execute("UPDATE provider_settings SET is_default = 0")
            conn.execute(
                """
                INSERT INTO provider_settings(
                    provider_id,
                    selected_model,
                    auth_mode,
                    secret_ref,
                    base_url,
                    reasoning_effort,
                    enabled,
                    is_default,
                    file_change_consent,
                    configured_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_id) DO UPDATE SET
                    selected_model = excluded.selected_model,
                    auth_mode = excluded.auth_mode,
                    secret_ref = excluded.secret_ref,
                    base_url = excluded.base_url,
                    reasoning_effort = excluded.reasoning_effort,
                    enabled = excluded.enabled,
                    is_default = excluded.is_default,
                    file_change_consent = excluded.file_change_consent,
                    configured_at = excluded.configured_at
                """,
                (
                    config.provider_id,
                    config.selected_model,
                    config.auth_mode.value,
                    config.secret_ref,
                    config.base_url,
                    config.reasoning_effort.value if config.reasoning_effort else None,
                    int(config.enabled),
                    int(config.is_default),
                    config.file_change_consent.value,
                    config.configured_at.isoformat(),
                ),
            )

    def get_provider_config(self, provider_id: str) -> ProviderConfig | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM provider_settings WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
        return self._provider_config_from_row(row) if row else None

    def get_default_provider_config(self) -> ProviderConfig | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM provider_settings
                WHERE enabled = 1 AND is_default = 1
                ORDER BY configured_at DESC
                LIMIT 1
                """
            ).fetchone()
        return self._provider_config_from_row(row) if row else None

    def list_provider_configs(self) -> list[ProviderConfig]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM provider_settings ORDER BY is_default DESC, provider_id"
            ).fetchall()
        return [self._provider_config_from_row(row) for row in rows]

    def record_provider_verification(
        self,
        provider_id: str,
        ok: bool,
        message: str,
        model: str | None = None,
    ) -> None:
        self.set_preference(
            f"provider_verification:{provider_id}",
            {
                "provider_id": provider_id,
                "ok": ok,
                "message": message,
                "model": model,
                "checked_at": datetime.now(UTC).isoformat(),
            },
        )

    def get_provider_verification(self, provider_id: str) -> dict[str, Any] | None:
        value = self.get_preference(f"provider_verification:{provider_id}")
        return value if isinstance(value, dict) else None

    def record_run(self, result: MatrixRunResult) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO runs(run_id, created_at, request, response, result_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    created_at = excluded.created_at,
                    request = excluded.request,
                    response = excluded.response,
                    result_json = excluded.result_json
                """,
                (
                    result.run_id,
                    result.created_at.isoformat(),
                    result.request,
                    result.response,
                    result.model_dump_json(),
                ),
            )
            for report in [result.preflight_report, result.output_report]:
                if report is None:
                    continue
                conn.execute(
                    """
                    INSERT INTO security_events(run_id, created_at, risk_level, approved, issues_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        result.run_id,
                        datetime.now(UTC).isoformat(),
                        report.risk_level.value,
                        int(report.approved),
                        json.dumps(report.issues),
                    ),
                )

    def record_model_call(
        self,
        provider_id: str,
        model: str,
        ok: bool,
        request_chars: int,
        response_chars: int,
        latency_ms: int | None = None,
        error_type: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO model_calls(
                    created_at,
                    provider_id,
                    model,
                    ok,
                    latency_ms,
                    request_chars,
                    response_chars,
                    error_type
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(UTC).isoformat(),
                    provider_id,
                    model,
                    int(ok),
                    latency_ms,
                    request_chars,
                    response_chars,
                    error_type,
                ),
            )

    def list_model_calls(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    id,
                    created_at,
                    provider_id,
                    model,
                    ok,
                    latency_ms,
                    request_chars,
                    response_chars,
                    error_type
                FROM model_calls
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_security_events(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, created_at, risk_level, approved, issues_json
                FROM security_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            record["issues"] = json.loads(row["issues_json"])
            del record["issues_json"]
            records.append(record)
        return records

    def set_preference(self, key: str, value: object) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO preferences(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(value), datetime.now(UTC).isoformat()),
            )

    def get_preference(self, key: str) -> object | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM preferences WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["value_json"])

    def _provider_config_from_row(self, row: sqlite3.Row) -> ProviderConfig:
        return ProviderConfig(
            provider_id=row["provider_id"],
            selected_model=row["selected_model"],
            auth_mode=row["auth_mode"],
            secret_ref=row["secret_ref"],
            base_url=row["base_url"],
            reasoning_effort=row["reasoning_effort"],
            enabled=bool(row["enabled"]),
            is_default=bool(row["is_default"]),
            file_change_consent=row["file_change_consent"],
            configured_at=datetime.fromisoformat(row["configured_at"]),
        )

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        column_name: str,
        column_type: str,
    ) -> None:
        columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        if not any(row["name"] == column_name for row in columns):
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
