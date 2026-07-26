from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_DEFAULT_IDENTITY = "You are nanobot, a lightweight AI agent assistant."


@dataclass
class ContextBuilder:
    workspace: str = "."
    bootstrap_files: list[str] = field(
        default_factory=lambda: ["AGENTS.md", "SOUL.md", "USER.md"]
    )

    def build_system_prompt(
        self, identity: str | None = None, session_summary: str | None = None
    ) -> str:
        parts: list[str] = []
        parts.append(identity if identity else _DEFAULT_IDENTITY)

        ws = Path(self.workspace)
        for filename in self.bootstrap_files:
            file_path = ws / filename
            if file_path.is_file():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        if session_summary:
            parts.append(f"[Archived Context Summary]\n\n{session_summary}")

        return "\n\n---\n\n".join(parts)

    def build_messages(
        self,
        current_message: str,
        history: list[dict[str, Any]] | None = None,
        identity: str | None = None,
        session_summary: str | None = None,
    ) -> list[dict[str, Any]]:
        system_content = self.build_system_prompt(identity=identity, session_summary=session_summary)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_content},
        ]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": current_message})
        return messages
