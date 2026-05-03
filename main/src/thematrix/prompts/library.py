from __future__ import annotations

from importlib import resources
from pathlib import Path


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

    def _safe_name(self, name: str) -> str:
        if "/" in name or "\\" in name or ".." in name:
            raise ValueError("Prompt names must be simple filenames.")
        if not name.endswith(".md"):
            return f"{name}.md"
        return name

