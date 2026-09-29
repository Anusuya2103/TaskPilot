"""Filesystem tools (sandboxed).

Every path is resolved and checked against the run's workspace. Overwriting an
existing file is flagged ``needs_approval`` by the policy engine because it
destroys prior data — the same rule a human would apply.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.errors import PolicyViolation, ToolError
from app.core.models import RiskLevel
from app.core.schemas import ToolResult
from app.core.security import sanitize_untrusted
from app.tools.base import Tool, ToolContext

MAX_READ_CHARS = 60_000
MAX_WRITE_CHARS = 200_000


def _sandbox(ctx: ToolContext, raw: str, *, must_exist: bool = False) -> Path:
    """Resolve ``raw`` inside the run workspace, refusing escapes."""
    if not raw or not str(raw).strip():
        raise ToolError("A file path is required")
    root = (ctx.artifact_dir / "workspace" / ctx.run_id[:8]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    candidate = Path(str(raw))
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not str(resolved).startswith(str(root)) and resolved != root:
        raise PolicyViolation(
            "Refusing to touch a path outside the run workspace",
            details={"requested": str(raw), "workspace": str(root)},
        )
    if must_exist and not resolved.exists():
        raise ToolError(f"File not found: {resolved.relative_to(root)}")
    return resolved


class FilesReadTool(Tool):
    name = "file_read"
    description = (
        "Read a text file from the run workspace (paths are relative to it). "
        "Use it to pick up captured tool output before writing a report."
    )
    risk_level = RiskLevel.LOW
    category = "files"

    @property
    def args_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path inside the workspace"},
                "max_chars": {"type": "integer", "default": 20000},
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    @property
    def examples(self) -> list[dict[str, Any]]:
        return [{"path": "tool-output.txt"}]

    async def _run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = _sandbox(ctx, str(args["path"]), must_exist=True)
        if path.is_dir():
            raise ToolError(f"{path.name} is a directory — use file_list instead")
        limit = min(int(args.get("max_chars") or MAX_READ_CHARS), MAX_READ_CHARS)
        raw = path.read_bytes()[: limit * 2]
        text = raw.decode("utf-8", errors="replace")
        sanitized = sanitize_untrusted(text, max_chars=limit)
        return ToolResult(
            ok=True,
            summary=f"Read {len(text)} chars from {path.name}",
            data={
                "path": str(path),
                "name": path.name,
                "text": sanitized.text,
                "truncated": sanitized.truncated,
                "security_flags": sanitized.flags,
            },
            extracted={"text": sanitized.text, "path": str(path)},
            needs_attention=sanitized.suspicious,
            reasons=sanitized.flags,
            untrusted_content=True,
        )


class FilesWriteTool(Tool):
    name = "file_write"
    description = (
        "Write a text file into the run workspace. Overwriting an existing file is "
        "destructive and will pause for human approval."
    )
    risk_level = RiskLevel.LOW
    category = "files"

    @property
    def args_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path inside the workspace"},
                "content": {"type": "string", "description": "Text to write"},
                "append": {"type": "boolean", "default": False},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        }

    @property
    def examples(self) -> list[dict[str, Any]]:
        return [{"path": "book-summary.md", "content": "# Top 5 books\n..."}]

    async def _run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = _sandbox(ctx, str(args["path"]))
        content = str(args["content"])
        if len(content) > MAX_WRITE_CHARS:
            raise ToolError(f"Content too large ({len(content)} chars > {MAX_WRITE_CHARS})")
        mode = "a" if args.get("append") else "w"
        existed = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open(mode, encoding="utf-8") as fh:
            fh.write(content)
        action = "Appended to" if args.get("append") else ("Overwrote" if existed else "Created")
        return ToolResult(
            ok=True,
            summary=f"{action} {path.name} ({len(content)} chars)",
            data={"path": str(path), "name": path.name, "bytes": len(content.encode())},
            extracted={"path": str(path), "written_chars": len(content)},
        )


class FilesListTool(Tool):
    name = "file_list"
    description = "List files in the run workspace (or a subdirectory of it)."
    risk_level = RiskLevel.LOW
    category = "files"

    @property
    def args_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": ".", "description": "Relative directory"},
            },
            "additionalProperties": False,
        }

    @property
    def examples(self) -> list[dict[str, Any]]:
        return [{"path": "."}]

    async def _run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        directory = _sandbox(ctx, str(args.get("path") or "."))
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
        entries = [
            {
                "name": p.name,
                "size": p.stat().st_size if p.is_file() else None,
                "type": "dir" if p.is_dir() else "file",
            }
            for p in sorted(directory.iterdir())[:200]
        ]
        return ToolResult(
            ok=True,
            summary=f"{len(entries)} entr(y/ies) in {directory.name}/",
            data={"path": str(directory), "entries": entries},
            extracted={"entries": entries},
        )


__all__ = ["FilesListTool", "FilesReadTool", "FilesWriteTool"]
