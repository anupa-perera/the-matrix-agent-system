from __future__ import annotations

from importlib import resources
from pathlib import Path
import re


class PromptLibrary:
    """Markdown-backed prompt loader.

    Package templates provide safe defaults. A user prompt directory can override them later
    without changing Python code.
    """

    def __init__(self, prompt_dir: Path | None = None):
        self.prompt_dir = prompt_dir

    def read(self, name: str) -> str:
        safe_name = self._safe_name(name)
        if self.prompt_dir:
            override_path = self.prompt_dir / safe_name
            if override_path.exists():
                return override_path.read_text(encoding="utf-8")
        return (
            resources.files("thematrix.prompts")
            .joinpath("templates", safe_name)
            .read_text(encoding="utf-8")
        )

    def install_defaults(self) -> None:
        if self.prompt_dir is None:
            return
        self.prompt_dir.mkdir(parents=True, exist_ok=True)
        template_root = resources.files("thematrix.prompts").joinpath("templates")
        for template in template_root.iterdir():
            if template.name.endswith(".md"):
                target = self.prompt_dir / template.name
                if not target.exists():
                    target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")

    def write_agent_blueprint(self, agent_id: str, content: str) -> Path | None:
        if self.prompt_dir is None:
            return None
        safe_id = self._safe_stem(agent_id)
        target = self.prompt_dir / "agents" / f"{safe_id}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def read_agent_blueprint(self, agent_id: str) -> str:
        if self.prompt_dir is None:
            raise FileNotFoundError("No prompt directory is configured.")
        safe_id = self._safe_stem(agent_id)
        target = self.prompt_dir / "agents" / f"{safe_id}.md"
        return target.read_text(encoding="utf-8")

    def _safe_name(self, name: str) -> str:
        if "/" in name or "\\" in name or ".." in name:
            raise ValueError("Prompt names must be simple filenames.")
        if not name.endswith(".md"):
            return f"{name}.md"
        return name

    def _safe_stem(self, value: str) -> str:
        stem = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip(".-")
        if not stem:
            raise ValueError("Prompt stems cannot be empty.")
        return stem[:120]
