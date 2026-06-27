"""Smoke test for JsonFileTracer — loads agent from YAML, triggers sub-agent.

Verifies that tracer declared in examples/json_tracer/agent.yaml produces
valid JSON trace files covering AGENT → SUB_AGENT → LLM chains.

Usage:
    uv run python scripts/demo_json_tracer.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv

from nexau.archs.main_sub.agent import Agent
from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.session import SessionManager
from nexau.archs.session.orm import InMemoryDatabaseEngine

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENT_YAML = Path(__file__).resolve().parent.parent / "examples" / "json_tracer" / "agent.yaml"
TRACES_DIR = Path(__file__).resolve().parent.parent / "examples" / "json_tracer" / "traces"

SpanDict = dict[str, Any]

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    # 1. 清理旧 trace 文件
    if TRACES_DIR.exists():
        for f in TRACES_DIR.iterdir():
            if f.suffix == ".json":
                f.unlink()
                print(f"  cleaned old trace: {f.name}")

    # 2. 从 YAML 加载 AgentConfig（含 sub_agent 和 tracer 配置）
    config = AgentConfig.from_yaml(AGENT_YAML)
    tracer_types = [type(t).__name__ for t in config.tracers]
    sub_agents = config.sub_agents or []
    sub_names: list[str] = [
        cast(dict[str, Any], s)["name"] if isinstance(s, dict) else str(s)
        for s in sub_agents
    ]
    print(f"Config loaded from: {AGENT_YAML}")
    print(f"  tracers:    {tracer_types}")
    print(f"  sub_agents: {sub_names}")

    # 3. 初始化 session
    engine = InMemoryDatabaseEngine()
    sm = SessionManager(engine=engine)
    await sm.setup_models()

    # 4. 创建 Agent
    agent = await Agent.create(
        config=config,
        session_manager=sm,
        user_id="test_user",
        session_id="json_tracer_test",
    )
    print(f"Agent created: {config.name}")

    # 5. 第 1 轮: 触发 sub-agent 调用
    #    根据 system prompt，agent 应该将事实类问题委托给 echo_assistant
    print()
    print("=" * 40)
    print("Turn 1: 触发 sub-agent (echo_assistant)")
    print("=" * 40)
    response = await agent.run_async(message="请帮我查一下，Python 编程语言是谁创建的？")
    print(f"Agent response: {response}")

    # 6. 第 2 轮: 直接对话（不使用 sub-agent）
    print()
    print("=" * 40)
    print("Turn 2: 普通对话（不触发 sub-agent）")
    print("=" * 40)
    response = await agent.run_async(message="一句话总结 Python 的特点。")
    print(f"Agent response: {response}")

    # 7. shutdown → tracer.flush() 落盘
    print()
    print("Shutting down agent → tracer.flush()...")
    agent.sync_cleanup()

    # 8. 验证 JSON trace 文件
    print()
    print("=" * 40)
    print("Verifying trace files...")
    json_files = sorted(TRACES_DIR.glob("*.json"))
    if not json_files:
        print("ERROR: No trace JSON files found!")
        sys.exit(1)

    span_types_found: set[str] = set()
    for json_file in json_files:
        print(f"\n  File: {json_file.name} ({json_file.stat().st_size} bytes)")

        with open(json_file, encoding="utf-8") as f:
            raw_traces = json.load(f)

        traces = cast(list[SpanDict], raw_traces)
        assert isinstance(traces, list), f"Expected list, got {type(traces)}"
        assert len(traces) > 0, "Expected at least one root span"

        for root in traces:
            span_types_found.add(str(root.get("type", "")))
            print(
                f"  Root: {root['name']} ({root['type']}) "
                f"— {root['duration_ms']:.1f}ms, "
                f"children={len(root.get('children', []))}"
            )
            _recursive_print(root, depth=1, types=span_types_found)
            _validate_trace(root)

    # 9. 检查是否捕获到 SUB_AGENT span
    print()
    print("=" * 40)
    print("Span types found:", sorted(span_types_found))
    if "SUB_AGENT" in span_types_found:
        print("✓ SUB_AGENT span captured — sub-agent tracing works!")
    else:
        print("⚠ SUB_AGENT span NOT found (agent may not have delegated)")

    print()
    print("✓ All checks passed!")
    print(f"  Trace files: {[f.name for f in json_files]}")


def _recursive_print(span: SpanDict, depth: int, types: set[str]) -> None:
    """递归打印 span 树结构。"""
    indent = "  " * depth
    children = cast(list[SpanDict], span.get("children", []))
    for child in children:
        types.add(str(child.get("type", "")))
        dur = child.get("duration_ms")
        dur_str = f"{dur:.1f}ms" if isinstance(dur, (int, float)) else "N/A"
        print(f"{indent}↳ {child['name']} ({child['type']}) — {dur_str}")
        _recursive_print(child, depth + 1, types)


def _validate_trace(span: SpanDict) -> None:
    """验证 span 结构的必要字段。"""
    assert "id" in span, "Missing id"
    assert "name" in span, "Missing name"
    assert "type" in span, "Missing type"
    assert "start_time" in span, "Missing start_time"
    assert isinstance(span["start_time"], float), f"start_time type: {type(span['start_time'])}"

    valid_types = {"AGENT", "SUB_AGENT", "LLM", "TOOL", "COMPACTION"}
    assert span["type"] in valid_types, f"Unknown span type: {span['type']}"

    if span.get("end_time") is not None:
        assert span["end_time"] >= span["start_time"]

    if span.get("parent_id") is None:
        assert span["type"] == "AGENT", f"Root span should be AGENT, got {span['type']}"

    for child in cast(list[SpanDict], span.get("children", [])):
        assert child["parent_id"] == span["id"], "Child parent mismatch"
        _validate_trace(child)


if __name__ == "__main__":
    asyncio.run(main())
