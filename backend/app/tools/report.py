"""Report tool: lets the *agent* produce a deliverable as a first-class step.

The human-facing report generator lives in :mod:`app.reports.generator`; this
tool is the agent's way of writing prose/charts into the run workspace so the
final `task_complete` answer can point at something concrete.
"""

from __future__ import annotations

from typing import Any

from app.core.errors import PolicyViolation, ToolError
from app.core.models import RiskLevel
from app.core.schemas import ToolResult
from app.tools.base import Tool, ToolContext

MAX_REPORT_CHARS = 120_000


def _workspace(ctx: ToolContext) -> Any:
    root = (ctx.artifact_dir / "workspace" / ctx.run_id[:8]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


class ReportWriteTool(Tool):
    name = "write_report"
    description = (
        "Write a Markdown report/deliverable to the run workspace. Use it as the "
        "last step before task_complete when the user asked for a written output. "
        "Overwriting an existing report requires approval."
    )
    risk_level = RiskLevel.LOW
    category = "report"

    @property
    def args_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Report title"},
                "markdown": {
                    "type": "string",
                    "description": "Full Markdown body (tables welcome)",
                },
                "filename": {
                    "type": "string",
                    "default": "report.md",
                    "description": "Output filename inside the workspace",
                },
            },
            "required": ["title", "markdown"],
            "additionalProperties": False,
        }

    @property
    def examples(self) -> list[dict[str, Any]]:
        return [
            {
                "title": "Top 5 books by price",
                "markdown": "# Top 5 books\n\n| # | Title | Price |\n|---|---|---|\n| 1 | ... |",
                "filename": "books-report.md",
            }
        ]

    async def _run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        title = str(args["title"])
        markdown = str(args["markdown"])
        if len(markdown) > MAX_REPORT_CHARS:
            raise ToolError(f"Report too large ({len(markdown)} chars)")

        root = _workspace(ctx)
        filename = str(args.get("filename") or "report.md")
        if "/" in filename or "\\" in filename:
            raise PolicyViolation("filename must not contain path separators")
        path = (root / filename).resolve()
        if not str(path).startswith(str(root)):
            raise PolicyViolation("Report path escapes the workspace")

        document = markdown if markdown.lstrip().startswith("#") else f"# {title}\n\n{markdown}"
        if not document.startswith(f"# {title}"):
            document = f"# {title}\n\n{document}"
        path.write_text(document, encoding="utf-8")
        ctx.state["report_path"] = str(path)
        ctx.state["report_markdown"] = document

        return ToolResult(
            ok=True,
            summary=f"Wrote report {filename} ({len(document)} chars)",
            data={
                "path": str(path),
                "filename": filename,
                "title": title,
                "chars": len(document),
                "preview": document[:1500],
            },
            extracted={"report": document, "path": str(path), "title": title},
        )


__all__ = ["ReportWriteTool"]
