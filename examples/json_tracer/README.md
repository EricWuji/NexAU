# JsonFileTracer 使用示例

## 概述

`JsonFileTracer` 是 NexAU tracer 体系中的本地持久化适配器。它将 agent 执行过程中的所有 span（AGENT、LLM、TOOL 等）以嵌套 JSON 格式写入本地文件，方便调试、审计和离线分析。

与 `LangfuseTracer`（远程 SaaS）互为替代/补充。

## 使用方式

### 方式 1: YAML 配置文件（推荐）

在 agent 配置的 `tracers:` 节点下声明：

```yaml
tracers:
  - import: nexau.archs.tracer.adapters.json_file:JsonFileTracer
    params:
      output_dir: traces        # 输出目录，默认 traces/
      pretty_print: true         # 是否格式化 JSON（默认 true）
      enabled: true              # 是否启用（默认 true）
```

### 方式 2: Python 代码直接构造

```python
from nexau.archs.tracer.adapters import JsonFileTracer
from nexau.archs.tracer.context import TraceContext
from nexau.archs.tracer.core import SpanType

# 创建 tracer
tracer = JsonFileTracer(output_dir="my_traces")

# 手动包裹业务逻辑
with TraceContext(tracer, "my_agent", SpanType.AGENT, inputs={"msg": "hello"}) as span:
    # ... agent 执行 ...
    span.set_outputs({"result": "done"})

# 落盘
tracer.flush()
# → my_traces/trace_<session_id>_<timestamp>.json
```

### 方式 3: 与 Langfuse 组合

同时配置两个 tracer，框架自动创建 `CompositeTracer` 将数据同时发送到 Langfuse 和本地 JSON：

```yaml
tracers:
  - import: nexau.archs.tracer.adapters.langfuse:LangfuseTracer
    params:
      public_key: ${env.LANGFUSE_PUBLIC_KEY}
      secret_key: ${env.LANGFUSE_SECRET_KEY}
      host: ${env.LANGFUSE_HOST}
  - import: nexau.archs.tracer.adapters.json_file:JsonFileTracer
    params:
      output_dir: traces
```

## 配置参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `output_dir` | `str` | `"traces"` | JSON 文件输出目录 |
| `pretty_print` | `bool` | `True` | 是否格式化输出（缩进 2 空格） |
| `enabled` | `bool` | `True` | `False` 时零开销，不存储不写入 |
| `session_id` | `str` | 自动生成 UUID | 会话标识，影响文件名 |

## 输出文件

### 文件命名

```
traces/trace_<session_id>_<timestamp>.json
```

示例: `traces/trace_sess_abc123_20260627T093015Z.json`

### JSON 结构

```json
[
  {
    "id": "uuid-ag-...",
    "name": "my_agent",
    "type": "AGENT",
    "parent_id": null,
    "start_time": 1751424000.123,
    "end_time": 1751424005.456,
    "duration_ms": 5333.0,
    "inputs": {"message": "Hello"},
    "outputs": {"result": "Hi there!"},
    "attributes": {},
    "error": null,
    "children": [
      {
        "id": "uuid-llm-...",
        "name": "llm_call",
        "type": "LLM",
        "parent_id": "uuid-ag-...",
        "start_time": 1751424000.200,
        "end_time": 1751424005.300,
        "duration_ms": 5100.0,
        "inputs": {
          "model": "claude-sonnet-4-5-20250929",
          "messages": [...]
        },
        "outputs": {
          "usage": {"input_tokens": 150, "output_tokens": 80}
        },
        "children": []
      }
    ]
  }
]
```

### Span 字段说明

| 字段 | 说明 |
|------|------|
| `id` | Span 唯一 ID (UUID) |
| `name` | Span 名称 |
| `type` | Span 类型: `AGENT` / `SUB_AGENT` / `LLM` / `TOOL` / `COMPACTION` |
| `parent_id` | 父 span ID（null 表示根 span） |
| `start_time` | 开始时间（Unix timestamp） |
| `end_time` | 结束时间（Unix timestamp） |
| `duration_ms` | 耗时（毫秒） |
| `inputs` | 输入数据 |
| `outputs` | 输出数据 |
| `attributes` | 附加元数据 |
| `error` | 错误信息（如有） |
| `children` | 子 span 列表（保持嵌套关系） |

## 运行示例

```bash
# 1. 设置 LLM 环境变量
export LLM_MODEL="claude-sonnet-4-5-20250929"
export LLM_BASE_URL="https://your-gateway.example.com"
export LLM_API_KEY="sk-..."

# 2. 运行 agent
nexau run examples/json_tracer/agent.yaml --message "列出当前目录的文件"

# 3. 查看输出
ls traces/
# → trace_<session_id>_20260627T093015Z.json

# 4. 用 jq 浏览
cat traces/trace_*.json | jq '.[] | {name, type, duration_ms, children: [.children[]? | {name, type, duration_ms}]}'
```
