from __future__ import annotations

import os
from pathlib import Path

from nexau import Agent, Plugin
from nexau.archs.main_sub.config.config import AgentConfig
from nexau.archs.session import InMemoryDatabaseEngine, SessionManager

_REQUIRED_ENV_KEYS = (
    "LLM_MODEL",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_API_TYPE",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_BASE_URL",
)


def _required_env() -> dict[str, str]:
    missing = [key for key in _REQUIRED_ENV_KEYS if not os.environ.get(key)]
    if missing:
        raise RuntimeError(f"Set {', '.join(missing)} before running this live quickstart.")
    return {key: os.environ[key] for key in _REQUIRED_ENV_KEYS}


def build_agent_config() -> AgentConfig:
    env = _required_env()
    script_dir = Path(__file__).resolve().parent
    coding_agent_plugin = Plugin.from_yaml(
        script_dir / "plugins" / "coding-agent" / "plugin.yaml",
        config={
            "project_name": "nexau",
            "default_branch": "main",
            "package_manager": "uv",
            "test_command": "uv run pytest",
            "llm_model": env["LLM_MODEL"],
            "llm_base_url": env["LLM_BASE_URL"],
            "llm_api_key": env["LLM_API_KEY"],
            "llm_api_type": env["LLM_API_TYPE"],
        },
        base_path=script_dir,
    )
    return AgentConfig.from_dict(
        {
            "type": "agent",
            "name": "plugin_adapter_coding_agent",
            "system_prompt": (
                "You are a pragmatic coding agent for repository maintenance.\n"
                "Use the coding agent plugin for codebase exploration, focused edits, "
                "test runs, and implementation planning."
            ),
            "llm_config": {
                "model": env["LLM_MODEL"],
                "base_url": env["LLM_BASE_URL"],
                "api_key": env["LLM_API_KEY"],
                "api_type": env["LLM_API_TYPE"],
            },
            "tracers": [
                {
                    "import": "nexau.archs.tracer.adapters.langfuse:LangfuseTracer",
                    "params": {
                        "public_key": env["LANGFUSE_PUBLIC_KEY"],
                        "secret_key": env["LANGFUSE_SECRET_KEY"],
                        "host": env["LANGFUSE_BASE_URL"],
                        "tags": [
                            "rfc-0024",
                            "plugin-adapter",
                            "coding-agent-example",
                        ],
                        "metadata": {
                            "example": "plugin_adapter",
                            "plugin": "example.coding-agent",
                        },
                    },
                },
            ],
            "tool_call_mode": "structured",
            "max_iterations": 4,
            "plugins": [coding_agent_plugin],
        },
        base_path=script_dir,
    )


def _print_config_summary(config: AgentConfig) -> None:
    print(f"loaded agent: {config.name}")
    print("mcp servers:")
    for server in config.mcp_servers:
        print(f"  - {server['name']} ({server.get('source_id')})")

    print("tools:")
    for tool in config.tools:
        print(f"  - {tool.name} ({tool.source_id})")

    print("skills:")
    for skill in config.skills:
        print(f"  - {skill.name} ({skill.source_id})")

    print("sub-agents:")
    for name, sub_agent in (config.sub_agents or {}).items():
        print(f"  - {name} ({sub_agent.source_id})")

    print("middlewares:")
    for middleware in config.middlewares or []:
        print(f"  - {middleware.__class__.__name__} ({middleware.source_id})")


def main() -> None:
    config = build_agent_config()
    _print_config_summary(config)

    agent = Agent(
        config=config,
        session_manager=SessionManager(engine=InMemoryDatabaseEngine()),
        user_id="plugin_adapter_quickstart",
        session_id="plugin_adapter_quickstart",
    )

    print("\nmode:")
    print("  - live LLM")
    print("\nagent.run response:")
    response = agent.run(
        message=(
            "Use only write_todos to create a short coding implementation plan with three steps: "
            "inspect the relevant code, apply a focused patch, and run verification. "
            "Do not call file or shell tools in this quickstart. Then summarize the plan."
        ),
    )
    print(response)


if __name__ == "__main__":
    main()
