"""JsonFileTracer 程序化使用示例。

演示如何在 Python 代码中直接使用 JsonFileTracer，不依赖 YAML 配置。
运行: python examples/json_tracer/run_example.py
"""

from nexau.archs.tracer.adapters import JsonFileTracer
from nexau.archs.tracer.context import TraceContext
from nexau.archs.tracer.core import SpanType


def main() -> None:
    # 1. 创建 tracer
    tracer = JsonFileTracer(
        output_dir="traces",
        pretty_print=True,
        enabled=True,
    )
    print(f"Tracer 初始化: session_id={tracer.session_id}")

    # 2. 使用 TraceContext 包裹业务逻辑，自动管理 span 层次
    with TraceContext(
        tracer,
        "my_agent",
        SpanType.AGENT,
        inputs={"message": "Hello, world!"},
    ) as agent_span:
        # ---- LLM call span ----
        with TraceContext(
            tracer,
            "llm_call",
            SpanType.LLM,
            inputs={"model": "claude-sonnet-4-5", "prompt": "Hello"},
        ) as llm_span:
            # 模拟 LLM 调用
            llm_span.set_outputs({
                "content": "Hi there!",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            })

        # ---- Tool call span ----
        with TraceContext(
            tracer,
            "read_file",
            SpanType.TOOL,
            inputs={"path": "/tmp/example.txt"},
        ) as tool_span:
            tool_span.set_outputs({"content": "Hello from file"})

        # 设置 agent 总体输出
        agent_span.set_outputs({"result": "done", "iterations": 1})

    # 3. 落盘
    tracer.flush()

    # 4. 查看内存中的 trace 树（也可以不 flush，先预览）
    # tree = tracer.get_trace_tree()
    # print(json.dumps(tree, indent=2, ensure_ascii=False))

    print(f"Span 统计: 总数={tracer.span_count}, 根 span={tracer.root_count}")


if __name__ == "__main__":
    main()
