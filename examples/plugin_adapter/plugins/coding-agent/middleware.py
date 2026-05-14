from __future__ import annotations

from dataclasses import dataclass

from nexau.archs.main_sub.execution.hooks import BeforeAgentHookInput, HookResult, Middleware


@dataclass(frozen=True)
class CodingContext:
    project_name: str
    default_branch: str
    package_manager: str
    test_command: str

    def as_dict(self) -> dict[str, str]:
        return {
            "project_name": self.project_name,
            "default_branch": self.default_branch,
            "package_manager": self.package_manager,
            "test_command": self.test_command,
        }


class CodingContextMiddleware(Middleware):
    """Publish plugin coding defaults into AgentState context for runtime hooks."""

    def __init__(
        self,
        project_name: str,
        default_branch: str,
        package_manager: str,
        test_command: str,
    ) -> None:
        self.context = CodingContext(
            project_name=project_name,
            default_branch=default_branch,
            package_manager=package_manager,
            test_command=test_command,
        )

    def before_agent(self, hook_input: BeforeAgentHookInput) -> HookResult:
        hook_input.agent_state.set_context_value("coding_plugin", self.context.as_dict())
        return HookResult.no_changes()
