from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from thematrix.schemas import AgentSpec, MatrixRunResult, SecurityReport


class MemoryVault:
    """Obsidian-compatible markdown vault for human-readable memory."""

    def __init__(self, root: Path):
        self.root = root

    def initialize(self) -> None:
        for relative in [
            "raw/requests",
            "raw/runs",
            "raw/tool_outputs",
            "raw/neo_reviews",
            "wiki/agents",
            "wiki/workflows",
            "wiki/decisions",
            "wiki/risks",
            "wiki/users",
            "schema",
        ]:
            (self.root / relative).mkdir(parents=True, exist_ok=True)

        self._write_once(
            "index.md",
            "# The Matrix Vault\n\n"
            "This vault is the human-readable memory for The Matrix Agent System.\n\n"
            "## Maps\n\n"
            "- [[log]]\n"
            "- [[schema/memory_rules]]\n"
            "- [[schema/agent_rules]]\n"
            "- [[schema/security_rules]]\n\n"
            "## Areas\n\n"
            "- `raw/` keeps immutable records.\n"
            "- `wiki/` keeps synthesized memory.\n"
            "- `schema/` keeps memory and safety rules.\n",
        )
        self._write_once("log.md", "# Log\n\n")
        self._write_once(
            "schema/memory_rules.md",
            "# Memory Rules\n\n"
            "- Raw records are append-only.\n"
            "- Wiki pages are synthesized summaries.\n"
            "- Sensitive personal details require care before saving.\n"
            "- Prompt cache metadata lives in SQLite; prompt text lives in markdown.\n",
        )
        self._write_once(
            "schema/agent_rules.md",
            "# Agent Rules\n\n"
            "- Oracle owns intent, ethics, and human nature.\n"
            "- Architect owns technical specs, memory scope, and reuse.\n"
            "- Neo owns security approval.\n"
            "- Sub-agents are coordinated by Architect, not by free-form chatter.\n",
        )
        self._write_once(
            "schema/security_rules.md",
            "# Security Rules\n\n"
            "- Agents do not read raw secrets.\n"
            "- Shell access is gated.\n"
            "- Local-only privacy blocks cloud providers.\n"
            "- Neo reviews specs before execution and output before user delivery.\n",
        )

    def record_run(self, result: MatrixRunResult) -> None:
        run_path = self.root / "raw" / "runs" / f"{result.run_id}.json"
        request_path = self.root / "raw" / "requests" / f"{result.run_id}.md"
        run_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        request_path.write_text(
            f"# Request {result.run_id}\n\n{result.request}\n",
            encoding="utf-8",
        )
        self.append_log(
            title=f"Run {result.run_id}",
            body=(
                f"Request: {result.request}\n\n"
                f"Agent: {result.agent_spec.agent_id if result.agent_spec else 'none'}\n\n"
                f"Response: {result.response}"
            ),
        )

    def record_agent_spec(self, spec: AgentSpec) -> None:
        agent_path = self.root / "wiki" / "agents" / f"{spec.agent_id}.md"
        agent_path.parent.mkdir(parents=True, exist_ok=True)
        agent_path.write_text(
            (
                f"# {spec.agent_id}\n\n"
                f"- Type: {spec.agent_type}\n"
                f"- Purpose: {spec.purpose}\n"
                f"- Risk: {spec.risk_level.value}\n"
                f"- Reusable: {spec.reusable}\n"
                f"- Reuse candidate: {spec.reuse_candidate_id or 'none'}\n"
                f"- Provider: {spec.provider_id}\n"
                f"- Model: {spec.model_id}\n\n"
                "## Capabilities\n\n"
                f"{self._markdown_list(spec.capabilities)}\n\n"
                "## Tools\n\n"
                f"{self._markdown_list(spec.tools_allowed)}\n\n"
                "## Memory Scope\n\n"
                f"{self._markdown_list(spec.memory_scope)}\n\n"
                "## Constraints\n\n"
                f"{self._markdown_list(spec.constraints)}\n\n"
                "## Prompt Blocks\n\n"
                f"{self._markdown_list(spec.prompt_block_refs)}\n"
            ),
            encoding="utf-8",
        )

    def record_security_review(
        self,
        run_id: str,
        stage: str,
        report: SecurityReport,
    ) -> None:
        safe_stage = "".join(char for char in stage if char.isalnum() or char in {"-", "_"})
        review_path = self.root / "raw" / "neo_reviews" / f"{run_id}-{safe_stage}.md"
        review_path.parent.mkdir(parents=True, exist_ok=True)
        review_path.write_text(
            (
                f"# Neo Review: {run_id} {safe_stage}\n\n"
                f"- Approved: {report.approved}\n"
                f"- Risk: {report.risk_level.value}\n\n"
                "## Issues\n\n"
                f"{self._markdown_list(report.issues)}\n\n"
                "## Required Changes\n\n"
                f"{self._markdown_list(report.required_changes)}\n"
            ),
            encoding="utf-8",
        )

    def append_log(self, title: str, body: str) -> None:
        timestamp = datetime.now(UTC).isoformat()
        log_path = self.root / "log.md"
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## {timestamp} - {title}\n\n{body}\n")

    def _write_once(self, relative_path: str, content: str) -> None:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(content, encoding="utf-8")

    def _markdown_list(self, values: list[str]) -> str:
        if not values:
            return "- None"
        return "\n".join(f"- {value}" for value in values)
