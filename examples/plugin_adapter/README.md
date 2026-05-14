# Plugin Adapter Example

This example shows a usable RFC-0024 local plugin loaded through:

```yaml
plugins:
  - use: "path:./plugins/coding-agent"
```

The plugin contributes a small but realistic coding-agent bundle:

- one local stdio MCP server: `coding_repo_context`
- common coding tools: `read_file`, `search_file_content`, `list_directory`,
  `run_shell_command`, `apply_patch`, `replace`, and `write_todos`
- always-on prompt guidance via top-level `system_prompt_fragment`
- development skills: `development-workflow` and `test-debug-loop`
- two sub-agents: `explore` and `worker`
- one middleware: `coding_context`

The plugin owns a static top-level `system_prompt_fragment`, so the host
agent's system prompt is extended without requiring the host agent to duplicate
the fragment text. The same fragment is also rendered into plugin-owned
sub-agent prompts.
The main agent and plugin-owned sub-agents share the same LLM settings through
top-level `agent.yaml` variables populated from `LLM_MODEL`, `LLM_BASE_URL`,
`LLM_API_KEY`, and `LLM_API_TYPE`. The main agent also registers a Langfuse
tracer from `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and
`LANGFUSE_BASE_URL`.

Run the programmatic smoke test:

```bash
LLM_MODEL=gpt-5.4 \
LLM_BASE_URL=http://localhost:3001/v1 \
LLM_API_KEY=... \
LLM_API_TYPE=openai_responses \
LANGFUSE_PUBLIC_KEY=... \
LANGFUSE_SECRET_KEY=... \
LANGFUSE_BASE_URL=https://cloud.langfuse.com \
uv run python examples/plugin_adapter/quickstart.py
```

Run the YAML-backed smoke test:

```bash
uv run python examples/plugin_adapter/quickstart_yaml.py
```

The quickstart calls `Agent.run(...)` with a real model. It requires the
`LLM_MODEL`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_API_TYPE`,
`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and `LANGFUSE_BASE_URL`
environment variables.

`quickstart.py` builds the host agent with `AgentConfig.from_dict(...)` and
enables the same path plugin through `Plugin.from_yaml(...)`. `quickstart_yaml.py`
contains the YAML-backed implementation that loads `agent.yaml` directly.
