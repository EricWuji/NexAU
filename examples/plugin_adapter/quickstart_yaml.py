from __future__ import annotations

import os
from pathlib import Path

from nexau import Agent
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


def _missing_required_env() -> list[str]:
    return [key for key in _REQUIRED_ENV_KEYS if not os.environ.get(key)]


def build_agent_config() -> AgentConfig:
    missing = _missing_required_env()
    if missing:
        raise RuntimeError(f"Set {', '.join(missing)} before running this live quickstart.")

    config_path = Path(__file__).with_name("agent.yaml")
    return AgentConfig.from_yaml(config_path)


def main() -> None:
    config = build_agent_config()
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

    agent = Agent(
        config=config,
        session_manager=SessionManager(engine=InMemoryDatabaseEngine()),
        user_id="plugin_adapter_quickstart",
        session_id="plugin_adapter_quickstart",
    )

    print(agent.config)

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
