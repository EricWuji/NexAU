from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

VerificationValue = str | list[str]

mcp = FastMCP(
    "coding-repo-context",
    instructions=(
        "Provides repository metadata and verification guidance for the "
        "RFC-0024 coding-agent plugin example."
    ),
)


def _context() -> dict[str, str]:
    return {
        "project_name": os.environ.get("CODING_AGENT_PROJECT_NAME", "unknown"),
        "default_branch": os.environ.get("CODING_AGENT_DEFAULT_BRANCH", "main"),
        "package_manager": os.environ.get("CODING_AGENT_PACKAGE_MANAGER", "uv"),
        "test_command": os.environ.get("CODING_AGENT_TEST_COMMAND", "uv run pytest"),
    }


@mcp.tool()
def get_project_context() -> dict[str, str]:
    """Return the coding-agent plugin project context."""
    return _context()


@mcp.tool()
def suggest_verification(scope: str = "focused") -> dict[str, VerificationValue]:
    """Return a practical verification command for a coding change."""
    context = _context()
    command = context["test_command"]
    if scope == "quick":
        command = f"{command} -q"
    return {
        "project_name": context["project_name"],
        "default_branch": context["default_branch"],
        "scope": scope,
        "command": command,
        "notes": [
            "Prefer targeted tests for narrow changes.",
            "Run the broader default command before handing off larger changes.",
        ],
    }


if __name__ == "__main__":
    mcp.run()
