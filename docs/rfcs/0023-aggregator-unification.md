# RFC-0023: Provider Stream Aggregator Unification

- **状态**: draft
- **优先级**: P2
- **标签**: `architecture`, `refactoring`, `dx`, `testing`
- **影响服务**: NexAU(`archs/llm/llm_aggregators/`、`archs/main_sub/execution/llm_caller.py`、`archs/main_sub/execution/middleware/agent_events_middleware.py`)
- **创建日期**: 2026-05-01
- **更新日期**: 2026-05-01

## 摘要

NexAU 当前同时维护**两套并行的 provider stream aggregator**——分别处理同一份 provider SSE 输出但产出不同形态:Set A(`llm_aggregators/`)产 AG-UI events 推前端 SSE,Set B(`llm_caller.py` 内 `*StreamAggregator`)产 ModelResponse dict 喂持久化路径。两套独立解析同一份流意味着每次 provider 演进都要改两遍,且两边输出在理论上可能漂移。本 RFC 计划把两套合并成单一 canonical aggregator——provider 解析只一份,既能 push AG-UI events 也能 finalize 出 ModelResponse,Set B 退役为 Set A 的扩展接口的实现。

> **粒度上下文**:本 RFC 是 [RFC-0022(Agent Run Action 事件溯源协议)](0022-agent-run-action-lifecycle-and-typed-blocks.md) 的**前置依赖**。RFC-0022 Phase 2(iter 级持久化)要求"前端 SSE 看到的 token 流"和"持久化进 RunAction 的 Message"必须**同源**;只有完成本 RFC 的合并,iter 级 chunk → aggregate 框架才在实现层不漏。

合并按三阶段推进,各自独立 PR 可独立 review:
1. **阶段 ①**(Parity 测试基建):录制 provider SSE fixture + 跑两套 aggregator 等价性断言,作为后续重构的安全网
2. **阶段 ②**(AG-UI events 缺口补齐):引入 `ModelCallFinishedEvent` 等 sidecar event,让 AG-UI 端能承载 ModelResponse 所有字段(`stop_reason` / `model_name` 等)
3. **阶段 ③**(Set B 退役):`*StreamAggregator` 改造为 Set A 的 `Aggregator` ABC 子类(或被其替代),`llm_caller.py` 内部消费同一接口

## 动机

### 1. 重复维护负担

每个 provider(Anthropic / OpenAI Chat Completions / OpenAI Responses / Gemini REST)都需要**两套独立的解析实现**:

| Provider | Set A 实现 | Set B 实现 |
|---------|-----------|-----------|
| Anthropic | `archs/llm/llm_aggregators/anthropic/anthropic_event_aggregator.py`(318 行) | `llm_caller.py:2570-` `AnthropicStreamAggregator`(~290 行) |
| OpenAI Chat | `openai_chat_completion/` | `llm_caller.py:2454-` `OpenAIChatStreamAggregator` |
| OpenAI Responses | `openai_responses/` | `llm_caller.py:2860-` `OpenAIResponsesStreamAggregator` |
| Gemini REST | `gemini_rest/` | `llm_caller.py:3028-` `GeminiRestStreamAggregator` |

每次 provider 协议演进(Anthropic 加 extended thinking、OpenAI 加 reasoning summary、Gemini 加新 part 类型),要改两遍才能让 SSE 端和持久化端同步。任何一边漏改都不会被编译器或单测当场抓到——只有运行时不一致才暴露。

### 2. 漂移风险与"软合并"现状

理论上两套独立解析可以漂移。实际代码里已经存在**部分软合并**——`agent_events_middleware.after_model()` hook 读取 `model_response.usage`(Set B 的输出)然后 emit `UsageUpdateEvent` 到 Set A 的事件流。这意味着 Set A 不是完全独立的,它在某些字段上**消费 Set B 的最终输出**,而不是从同一份原始流自己导出 usage。

这种半绑定状态比"完全独立"更危险:
- 如果 Set B 改了 usage 字段名,Set A middleware 静默失败
- 没有强约束保证两边对同一份 SSE 流的解析在内容 block 维度等价

### 3. RFC-0022 Phase 2 阻塞

RFC-0022 §依赖关系 已显式声明 Phase 2 阻塞在本 RFC §阶段 ③ 完成。iter 级持久化要求每条 APPEND 写入的 Message 和前端 SSE 同步看到的 token 流来自同一次解析,否则 chunk → aggregate framing 在实现层有泄漏。

## 非目标

1. **不改 AG-UI 协议本身**——本 RFC 不改动 `ag_ui` 上游标准,新增的字段全部走 nexau 自己的 events 扩展(`nexau/archs/llm/llm_aggregators/events.py`)。
2. **不改 `ModelResponse` / `Message` 公开形态**——对外契约保持稳定,本 RFC 只重组实现路径。
3. **不引入新的 LLM provider**——本 RFC 处理现有 4 个 provider 的合并,不扩展 provider 范围。
4. **不实现 iter 级持久化**——那是 RFC-0022 Phase 2 的范围。本 RFC 只是它的前置条件。
5. **不重写 `agent_events_middleware`**——它依然是事件回调路由器;本 RFC 只是让它消费更完整的事件流而已。

## 现状审计

### A. Set A 详情(`archs/llm/llm_aggregators/`)

**结构**:

```python
class Aggregator[InputT, OutputT](ABC):
    def aggregate(self, item: InputT) -> None: ...
    def build(self) -> OutputT: ...
    def clear(self) -> None: ...

class AnthropicEventAggregator(Aggregator[RawMessageStreamEvent, None]):
    """Set A 的典型实现 — push 模式,build() 永远 return None"""
    def __init__(self, *, on_event: Callable[[Event], None], run_id: str) -> None: ...
```

**输出**:经 `on_event(Event)` 回调推 nexau 扩展后的 ag_ui events,**不返回 ModelResponse**。

**调用方**:

```
Set A → agent_events_middleware (唯一调用方)
              ↓
          on_event 回调推前端 SSE
```

**`build()` 返回 None**——Set A 是纯 push-style,没有"finalize 出最终对象"的概念。

### B. Set B 详情(`llm_caller.py` 内 `*StreamAggregator`)

**结构**(以 Anthropic 为例):

```python
class AnthropicStreamAggregator:
    def __init__(self) -> None:
        self.role: str = "assistant"
        self.model_name: str | None = None
        self.usage: dict[str, Any] | None = None
        self.stop_reason: str | None = None
        self._active_blocks: dict[int, dict[str, Any]] = {}
        self._completed_blocks: list[dict[str, Any]] = []

    def consume(self, event: Any) -> None: ...

    def finalize(self) -> dict[str, Any]:
        # 返回 ModelResponse-shaped dict:
        # { role, content, model, stop_reason, usage }
```

**输出**:`finalize() -> dict[str, Any]`,字段:`role` / `content` (block list) / `model` / `stop_reason` / `usage`。

**调用方**:**只在 `llm_caller.py` 内部使用**,出口是 `ModelResponse.from_anthropic_message(payload)` 等。

**没有 ABC 继承**,自定义类。

### C. ag_ui events 扩展性 ✅ 已具备

```python
# ag_ui/core/types.py
class ConfiguredBaseModel(BaseModel):
    model_config = ConfigDict(
        extra="allow",         # ← 任何 nexau-specific 字段都可加
        alias_generator=to_camel,
        populate_by_name=True,
    )

# ag_ui/core/events.py
class BaseEvent(ConfiguredBaseModel):
    type: EventType
    timestamp: Optional[int] = None
    raw_event: Optional[Any] = None    # ← 设计上就是给 provider 原 payload 用
```

可用的扩展机制(任选其一):

1. **`extra='allow'` 直接加字段**——动 BaseEvent 子类无成本,但 reader 拿不到类型提示
2. **`raw_event: Any`**——透传 provider 原 payload,各 reader 自己 unpack
3. **`CustomEvent(name, value)`**——nexau 自定义事件包装器
4. **新增 nexau-specific event 类型**——延续 nexau 已有先例(`UsageUpdateEvent` / `CompactionStartedEvent` 等)

### D. nexau 已经在 ag_ui 之上的扩展(已有先例)

`nexau/archs/llm/llm_aggregators/events.py` 已经定义:

```python
# 扩展的 ag_ui 子类(添加 nexau 字段)
TextMessageStartEvent extends AgUiTextMessageStartEvent
ThinkingTextMessageStartEvent extends AgUiThinkingTextMessageStartEvent
RunStartedEvent extends AgUiRunStartedEvent
RunErrorEvent extends AgUiRunErrorEvent

# 全新的 nexau 事件类型
UsageUpdateEvent(run_id, usage: TokenUsage)        # type="USAGE_UPDATE"
CompactionStartedEvent / CompactionFinishedEvent
TransportErrorEvent
UserMessageEvent / TeamMessageEvent                # RFC-0002 团队事件
ImageMessageStartEvent / ContentEvent / EndEvent   # 多模态
ToolCallResultEvent                                 # 替代 ag_ui 标准版

# 自己的 union type
Event = TextMessageStartEvent | ... | UsageUpdateEvent
```

**结论**:nexau 早已在 ag_ui 之上做扩展。本 RFC 加新的 sidecar event 类型(如 `ModelCallFinishedEvent`)是**既有模式的延续**,不破坏惯例。

### E. Gap 矩阵:Set B 输出 vs Set A 现有 events

| Set B `finalize()` 字段 | Set A 当前承载方式 | Gap 大小 |
|-----------------------|------------------|---------|
| `role` | `TextMessageStartEvent.role` | ✅ 已有 |
| `content`(blocks)| 各 ContentEvent 增量累积 | ✅ 已有 |
| `usage`(token 计数)| `UsageUpdateEvent.usage`(由 middleware 二次传递,**不是** aggregator 自己产出)| ⚠️ 现存依赖 Set B,合并后要让 aggregator 自己产 |
| `stop_reason` | ❌ 无 | **要加** |
| `model_name` / `model` | ❌ 无 | **要加** |
| `id`(provider message id)| `TextMessageStartEvent.message_id` 间接对应 | ⚠️ 部分 |
| Reasoning `signature`(Claude extended thinking)| ❌ ag_ui 标准 ThinkingTextMessage 无此字段 | **要加** |
| Reasoning `redacted_data`(Claude RedactedThinking)| ❌ 同上 | **要加** |

**Gap 不大**。合理补法:

- **新增 `ModelCallFinishedEvent`**(对照 `UsageUpdateEvent` 既有先例)在 LLM 调用结束时 emit,承载 `stop_reason` / `model_name` / `id` / 任何 provider-specific metadata。
- **扩展 `ThinkingTextMessageStart/EndEvent`** 加 `signature` / `redacted_data` 字段(走 ag_ui `extra='allow'`)。

### F. Fixture 现状

**已有**:

- `tests/fixtures/token_usage_regression.yaml` — 单一 yaml,token usage 回归用例,**不含真实 SSE 流**
- `tests/scripts/generate_llm_aggregator_logging_data.py` — 调用真实 API 落盘 events 的脚本,但**输出未 commit**(写到 `tests/test_data/llm_aggregators/`,该目录不存在于仓库)

**没有**:

- 录制的 provider 原始 SSE 字节流(任何形态)
- VCR / pytest-recording 的 cassette
- Set A / Set B 共享的 corpus

**两套现有单测各自规模**:

| 文件 | 行数 | 归属 |
|-----|-----|-----|
| `test_anthropic_event_aggregator.py` | 761 | Set A |
| `test_openai_chat_completion_aggregator.py` | 1098 | Set A |
| `test_openai_responses_aggregator.py` | 1533 | Set A |
| `test_anthropic_stream_else_branch.py` | 298 | Set A |
| `test_llm_streaming.py` | 364 | Set B |
| `test_llm_caller_async_stream.py` | 920 | Set B |
| **合计** | **~5000** | |

各自手工合成 SDK type 实例(`RawMessageStartEvent(...)` 这样硬编码),不是录制流。**没有共享 corpus** 是后续 parity 测试要补的。

### G. 开源 SSE corpus 调研结论

**无现成可拿的录制 corpus**。调查的 repo / 项目:

- `anthropics/anthropic-sdk-python/tests/test_streaming.py`:只测低级 SSE parser,inline 字节序列
- `openai/openai-python` tests:相同模式
- `BerriAI/litellm` / `langchain-ai/langchain`:无公开 cassette corpus
- `vcrpy` / `pytest-recording`:工具本身,不带 LLM 专用 corpus

**结论**:自己录最务实——扩展 `generate_llm_aggregator_logging_data.py` 让它输出真实 provider 原始 SSE 字节流,redact 后 commit 到 `tests/fixtures/provider_streams/`。

## 设计

### 方案 ①(推荐):Set A 一统,Set B 退役

让 Set A 的 `Aggregator` ABC 增加 `finalize() -> ModelResponse`(或者新增 sibling 接口 `Finalizable[T]`),Set A 的每个 provider 实现既 push events 又内部累积状态;`llm_caller.py` 用 `aggregator.finalize()` 取代 `*StreamAggregator.finalize()`。

```
旧:
   provider SSE
        ├─→ Set A AnthropicEventAggregator → on_event → ag_ui events
        └─→ Set B AnthropicStreamAggregator → finalize() → dict → ModelResponse

新:
   provider SSE
        └─→ unified AnthropicEventAggregator
                  ├─→ on_event 推 ag_ui events(同前)
                  └─→ finalize() 返 ModelResponse(取代 Set B)
```

**接口形状**(改 `Aggregator` ABC):

```python
class Aggregator[InputT, OutputT](ABC):
    @abstractmethod
    def aggregate(self, item: InputT) -> None: ...

    @abstractmethod
    def build(self) -> OutputT: ...    # 返回类型放宽,各实现可返 ModelResponse 或 None

    @abstractmethod
    def clear(self) -> None: ...
```

或者更精确——区分 push-only 和 finalizable 两种用法:

```python
class Aggregator[InputT, OutputT](ABC):
    def aggregate(self, item: InputT) -> None: ...
    def build(self) -> OutputT: ...

class AnthropicEventAggregator(Aggregator[RawMessageStreamEvent, ModelResponse]):
    def __init__(self, *, on_event: Callable[[Event], None] | None = None, run_id: str): ...
    # build() 返回 ModelResponse;同时通过 on_event(若提供)推 events
```

`on_event` 变成 optional,这样:
- 当调用方只关心持久化(Set B 老调用模式),不传 `on_event`,只调 `build()`
- 当调用方需要双输出(unified mode),既传 `on_event` 也调 `build()`

**优点**:
- provider 解析逻辑只有一份,绝无漂移
- 接口扩展自然,延续现有 ABC
- Set B 老调用方可以平滑迁移(只换 import 和 build 调用)

**缺点**:
- 单个 class 同时管 push events + finalize state,体量增大(Anthropic 合并后预计 ~500 行)
- 测试要覆盖两个输出端口都正确(parity test 解决)

### 方案 ②(备选):抽 provider parser,Set A / Set B 各自消费

```
provider SSE
    ↓
ProviderParser(共享,每 provider 一个,只解析,不输出)
    ↓ 中性 normalized event
    ├─→ AGUIBuilder → ag_ui events
    └─→ ModelResponseBuilder → ModelResponse
```

**优点**:
- 解析与下游构建分离,职责单一
- 中性中间事件可能为未来 provider 抽象提供基础

**缺点**:
- 需要设计一份**完备的中性中间事件 schema**——这个 schema 实际上就是 ag_ui events 自己,所以方案 ② 退化为方案 ① 的复杂版
- 多一层间接,无明显收益

**结论**:**采用方案 ①**。中性中间事件 schema 这件事,ag_ui events 加上 nexau 扩展实际上已经在做——再抽一层中间表示是 over-engineering。

### sidecar metadata events 设计

为补齐 Gap,新增以下 events(放 `nexau/archs/llm/llm_aggregators/events.py`,延续 `UsageUpdateEvent` 既有模式):

```python
class ModelCallFinishedEvent(BaseEvent):
    """LLM call 完成时 emit 一次,承载 ModelResponse 需要但 ag_ui 标准 events 没的字段。"""

    type: Literal["MODEL_CALL_FINISHED"] = "MODEL_CALL_FINISHED"
    run_id: str
    llm_call_id: str | None = None       # provider 返回的 message/response id
    model_name: str | None = None
    stop_reason: str | None = None
    finish_reason: str | None = None     # OpenAI 用 finish_reason
    raw_metadata: dict[str, Any] | None = None  # provider 特定的额外字段(cache_creation_input_tokens 等)
```

**emit 时机**:aggregator `finalize()` 执行前,确保 ag_ui 流末尾包含完整元信息。

**ThinkingTextMessage events 字段补齐**(走 `extra='allow'` 直接加):

```python
class ThinkingTextMessageStartEvent(AgUiThinkingTextMessageStartEvent):
    # 已有字段沿用
    # 新加:
    signature: str | None = None
    is_redacted: bool = False

class ThinkingTextMessageEndEvent(AgUiThinkingTextMessageEndEvent):
    redacted_data: str | None = None
```

> **决策**:为何不直接在 ag_ui `extra='allow'` 上塞 `stop_reason` / `model_name` 而要新建 event 类型?
>
> 因为 `stop_reason` / `model_name` 是**整次 LLM call 的元信息**,不属于任何具体 message/block——塞到 `MessageEndEvent.extra` 上语义不对。新建独立 event 类型清晰分离"消息内容"和"调用元信息"。

### 不变量 / 契约

`finalize()` 之前必须先把所有 provider raw events 通过 `aggregate()` 喂完,否则 `finalize()` 行为未定义(同当前 Set B `_completed_blocks` 不完整时的行为)。本 RFC 不放宽这个约束。

emit 顺序保证:
- `RunStartedEvent` 先于任何 message-level event
- `ModelCallFinishedEvent` 必须**晚于**所有 message-level event,**早于** `RunFinishedEvent`
- `finalize()` 只能调一次

## 阶段实施计划

### 阶段 ① — Parity 测试基建(独立 PR-A)

**目的**:在动 Set A / Set B 任何一行实现代码之前,先建立"两套等价"的强约束,作为后续重构的安全网。

**输出**:

```
tests/aggregator_parity/
├── fixtures/
│   ├── anthropic/
│   │   ├── plain_text.txt          ← 纯文本响应
│   │   ├── tool_calls.txt          ← 单/并行 tool 调用
│   │   ├── extended_thinking.txt   ← Claude extended thinking
│   │   ├── redacted_thinking.txt   ← redacted thinking
│   │   └── long_context.txt        ← 长上下文(prompt cache 命中场景)
│   ├── openai_chat/
│   ├── openai_responses/
│   └── gemini/
├── conftest.py
├── test_anthropic_parity.py
├── test_openai_chat_parity.py
├── test_openai_responses_parity.py
├── test_gemini_parity.py
├── strategies.py                    ← Hypothesis 生成器(如果做)
└── reconstructor.py                 ← AG-UI events → Message 参考实现
```

**Fixture 录制流程**:

1. 扩展 `tests/scripts/generate_llm_aggregator_logging_data.py`,加 `--dump-raw-sse` 模式,把 provider 原始 SSE byte stream 写到磁盘
2. 用预设 prompt(覆盖 5 个典型场景 × 4 provider = 20 条 fixture)跑一次真实 API
3. **redact**:删 API key、user-identifying 内容,占位符替换
4. commit fixture 到 `tests/aggregator_parity/fixtures/`

**Parity 测试断言**:

```python
def test_anthropic_parity(fixture_path: Path):
    raw_sse = fixture_path.read_bytes()

    # Set B 路径
    model_response_dict = run_set_b_anthropic(raw_sse)
    msg_from_b = Message.from_model_response_dict(model_response_dict)

    # Set A 路径
    agui_events = collect_agui_events(run_set_a_anthropic(raw_sse))
    msg_from_a = reconstruct_message_from_agui(agui_events)

    # 强等价(必须满足)
    assert msg_from_a.role == msg_from_b.role
    assert blocks_semantic_equal(msg_from_a.content, msg_from_b.content)

    # 弱等价(目前 Set A 缺,记录 gap,不阻断)
    record_gap("usage", missing_in=msg_from_a, present_in=msg_from_b)
    record_gap("stop_reason", ...)
    record_gap("model_name", ...)
```

**强等价**(必须断言):role / content blocks(数量 / 顺序 / 类型 / 字段)。
**弱等价**(记录 gap):usage / stop_reason / model_name / signature / redacted_data。

**第三轴 — Vendor Truth 等价**:

Set A vs Set B 互相一致是必要但**不充分**的。两者可能"一起错"——都和 vendor 自己在非流式调用下的 aggregation 不一致。这条风险的具体表现是 **prompt cache 命中率**:聚合后的 assistant `Message` 在下一轮(history compaction、multi-turn tool loop、agent-of-agent)被回放给 vendor 时,字节形态必须和 vendor 本身在 non-stream 调用下产生的 response 一致——否则 prompt cache prefix 失配,延迟 / 成本静默回退,没有任何测试会发现。

为此 §阶段 ① 在同一个目录下补:

- `scripts/record_fixture.py --also-non-stream`:录 SSE 的同时,用同样 prompt 再发一次 `stream:false` 请求,落 `<scenario>.non_stream.json` 到 `recordings/` 目录;Anthropic / OpenAI Chat / OpenAI Responses 通过 body 的 `stream:false` 切换,Gemini 通过 path 切换 (`streamGenerateContent` → `generateContent`)。
- `tests/aggregator_parity/test_stream_vs_non_stream.py`:自动发现任意 `<scenario>.sse` + `<scenario>.non_stream.json` 配对,SSE 喂 Set A → reconstructor → `Message`,non-stream JSON 走 `<provider>_non_stream_json_to_message` → `Message`,断言强等价。
- 真正的 vendor-side 设计差异(不是 bug 是设计取舍)登记进 `KNOWN_VENDOR_TRUTH_DIVERGENCES`,需要书面理由,strict xfail。


**输出物**:阶段 ① merge 后,`pytest tests/aggregator_parity/` 全绿,`tests/aggregator_parity/gap_report.md` 列出当前 Set A 缺的字段——这份报告是阶段 ② 的输入。

**安全网作用**:阶段 ② 改了 Set A 的输出后,parity test 会立即检测到强等价是否仍然成立;阶段 ③ 删掉 Set B 后,parity test 自动转换为"内部一致性测试"(因为只剩一套 aggregator,parity 自动满足)。

### 阶段 ② — AG-UI events 缺口补齐(独立 PR-B)

**目的**:让 Set A 的 events 流能承载 ModelResponse 所有字段,为阶段 ③ 的合并做准备。

**动作**:

1. 在 `nexau/archs/llm/llm_aggregators/events.py` 新增 `ModelCallFinishedEvent`、扩展 `ThinkingTextMessageStart/EndEvent` 字段
2. 4 个 provider 的 Set A aggregator 实现 emit 这些新 event(在 LLM call 结束时,从内部状态导出)
3. parity test(阶段 ① 建立的)弱等价部分**升级为强等价** —— Set A 现在不应再 missing usage / stop_reason / model_name 等字段
4. `agent_events_middleware.after_model()` 不再从 `model_response` 读 usage,改为订阅 `ModelCallFinishedEvent`(去掉对 Set B 的隐式依赖)

**风险**:`agent_events_middleware` 修改可能影响前端 SSE 行为。前端若依赖现有 `UsageUpdateEvent` 形态,要确认兼容性——`UsageUpdateEvent` 保留,只是 emit 来源换成 aggregator 而非 middleware。

### 阶段 ③ — Set B 退役(独立 PR-C)

**目的**:删除 Set B,让 Set A 成为唯一 provider stream parser。

**动作**:

1. 修改 `Aggregator` ABC 让 `build()` 返回类型对每个 provider 实现是 `ModelResponse`(或扩展 sibling 接口 `Finalizable[T]`)
2. 4 个 Set A aggregator 内部累积 ModelResponse 状态(参考 Set B 当前实现迁移)
3. `llm_caller.py` 内的 4 个 `*StreamAggregator` class **删除**,调用点改为创建 Set A aggregator 并调 `build()`
4. `agent_events_middleware` 自然继续工作——现在和 `llm_caller` 共享同一个 aggregator 实例
5. 删除 Set B 单测(`test_llm_streaming.py` 大部分、`test_llm_caller_async_stream.py` 中相关部分)
6. parity test 自然变成"单一 aggregator 自洽性测试"(只剩一条路径,parity 自动满足)

**Anthropic 合并后预估 ~500 行**(Set A 当前 318 + Set B 当前 ~290,合并去重后估计减少 30%)。其他 provider 类似比例。

**§阶段 ③ merge 验收门(Acceptance Criteria)**:

合并 PR-C 之前,以下三轴必须全绿——任何一轴红灯都说明 Set B 的某种行为在 Set A 上没有等价覆盖,贸然删除会引入回归。

1. **Set A vs Set B 强等价**(阶段 ① 主断言):`pytest tests/aggregator_parity/test_*_parity.py` 0 strong failure;0 weak gap(阶段 ② 应已全部关闭,残留意味着 §阶段 ② 没收尾)。
2. **Set A vs Vendor Non-Stream 强等价**(阶段 ① 第三轴):`pytest tests/aggregator_parity/test_stream_vs_non_stream.py` 0 strong failure;`KNOWN_VENDOR_TRUTH_DIVERGENCES` 中的每条 entry 都必须有书面理由 + strict xfail,Reviewer 显式签字承认每条 divergence 是设计取舍而非 bug。
3. **Vendor truth fixture 覆盖广度**:每个 provider 至少有 ≥ 3 条 `<scenario>.non_stream.json` 配对,覆盖 plain text / tool call / reasoning(及 redacted reasoning,如该 provider 支持)三个典型场景;否则 §阶段 ③ 等于在没有 ground truth 的情况下盲删。


**RFC-0022 Phase 2 解锁**:阶段 ③ merge 后,`AgentRunner` 可在每个 LLM iter 完成时,从同一个 aggregator 直接拿 ModelResponse → Message → APPEND,实现 iter 级持久化。

## 权衡取舍

### 考虑过的替代方案

1. **方案 ②(抽 provider parser 中性表示)**:见 §设计。被否——中性中间事件 schema 实际就是 ag_ui events,再抽一层是 over-engineering。

2. **维持现状,只加 parity test 不合并**:被否——重复维护负担不解决,RFC-0022 Phase 2 永久阻塞,parity test 长期也是"两套都对"而不是"单一 source",信息密度低。

3. **完全采用 ag_ui 上游标准,不加 nexau-specific events**:被否——Reasoning signature、redacted thinking、Anthropic cache token、provider-specific stop_reason 这些 nexau 业务必需的字段 ag_ui 上游不会接受(它是 UI 协议)。`extra='allow'` + nexau 扩展 events 是合理边界。

4. **把 `stop_reason` / `model_name` 塞到 `MessageEndEvent.extra` 而不新增 `ModelCallFinishedEvent`**:被否——`stop_reason` 是整次 LLM call 的属性,不属于任何单条 message;塞 MessageEndEvent 语义混乱。

### 缺点

- **合并后单个 aggregator class 体量翻倍**(预计 ~500 行 / provider)。可通过把内部状态管理拆成 helper 类(`MessageBuilder` / `ToolCallBuilder` 等,Set B 已有这种 helper)缓解。
- **阶段 ① 录制 fixture 需要真实 API key**——CI 跑录制好的 fixture 不需要 key,但首次录制和后续 provider 协议变更时要重新录制。这点和现有 `generate_llm_aggregator_logging_data.py` 一样,不算新负担。
- **阶段 ② / ③ 之间存在中间状态**——阶段 ② 完成但阶段 ③ 未完成时,Set B 仍存在,只是 Set A 可以独立产出 ModelResponse 等价信息。需要保证这段时间 Set B 仍是 ModelResponse 的 source of truth(parity test 帮忙)。

## 实现计划

### 阶段划分

- [ ] **阶段 ① — Parity 测试基建(PR-A)**
  - `tests/aggregator_parity/` 目录搭建
  - `tests/scripts/generate_llm_aggregator_logging_data.py` 加 `--dump-raw-sse`
  - 录制 4 provider × 5 场景 = 20 条 fixture
  - 4 个 parity test 文件(强等价 + 弱等价 gap 报告)
  - `gap_report.md` 输出
  - **第三轴**:`scripts/record_fixture.py --also-non-stream`(4 provider 全覆盖)+ `test_stream_vs_non_stream.py` + `NON_STREAM_LOADERS` + `KNOWN_VENDOR_TRUTH_DIVERGENCES` 注册表(空表合并,后续 fixture 录制后填充);录制目标 4 provider × ≥3 场景 = ≥12 条 `<scenario>.non_stream.json` 配对
- [ ] **阶段 ② — AG-UI events 缺口补齐(PR-B)**
  - 新增 `ModelCallFinishedEvent`、扩展 `ThinkingTextMessage*` 字段
  - 4 provider Set A aggregator emit 新 events
  - `agent_events_middleware` 改为订阅 `ModelCallFinishedEvent`(去 Set B 依赖)
  - parity test 弱等价升级为强等价
- [ ] **阶段 ③ — Set B 退役(PR-C)**
  - `Aggregator` ABC 调整(`build()` 返回 ModelResponse 或加 sibling 接口)
  - 4 个 Set A aggregator 内部累积 ModelResponse 状态
  - 删除 `llm_caller.py` 内 4 个 `*StreamAggregator`
  - 调用点改造
  - 删除 Set B 单测
  - **解锁 RFC-0022 Phase 2**

### 相关文件

- `nexau/archs/llm/llm_aggregators/events.py` — 新增 `ModelCallFinishedEvent`(阶段 ②)
- `nexau/archs/llm/llm_aggregators/anthropic/anthropic_event_aggregator.py` — emit 新 events + finalize ModelResponse(阶段 ②③)
- 同上 OpenAI Chat / Responses / Gemini(共 4 个 provider 文件夹)
- `nexau/archs/main_sub/execution/llm_caller.py` — 删除 4 个 `*StreamAggregator`(阶段 ③)
- `nexau/archs/main_sub/execution/middleware/agent_events_middleware.py` — 改 usage 订阅来源(阶段 ②)
- `tests/aggregator_parity/` — 全新目录(阶段 ①)
- `tests/scripts/generate_llm_aggregator_logging_data.py` — 扩展(阶段 ①)

### 测试方案

阶段 ① 自身就是测试基建。后续阶段:

- 阶段 ② 完成后,parity test 强等价范围扩大,任何回退会立刻被抓到
- 阶段 ③ 完成后,parity test 自然变为"内部一致性测试"(只剩一套 aggregator,parity 自动满足);保留这些 fixture-driven 测试作为 provider 协议演进的回归保护

阶段 ① 的 fixture 录制需要的真实 API key 配置同 `generate_llm_aggregator_logging_data.py`,本 RFC 不引入新的 secret 管理负担。

## 未解决的问题

1. **`Aggregator` ABC 返回类型签名最终形态**:`build() -> OutputT` 类型放宽,还是新增 sibling `Finalizable[T]` 接口?Phase ③ 实现时拍板。
2. **阶段 ② 中 `agent_events_middleware.after_model` 的过渡期行为**:在阶段 ② merge 但阶段 ③ 未完成时,middleware 是否应该同时支持订阅新 `ModelCallFinishedEvent` 和读 `model_response.usage` 作为 fallback?需要决定。
3. **Fixture redaction 的具体规则**:user content / assistant content 中可能包含敏感测试数据,redaction 需要规则手册。倾向"测试 prompt 用公开/中性内容"避免 redaction 复杂度。
4. **`raw_event` 字段是否被各 provider aggregator 主动塞**:`BaseEvent.raw_event` 可承载 provider 原 payload,但 Set A 当前各实现没塞这个字段。是否需要在阶段 ② 强制各 provider emit 时填 `raw_event = 原始 SSE chunk`?这会让 fixture 调试更直接,但也增加序列化体积。

## 参考资料

- [RFC-0022](0022-agent-run-action-lifecycle-and-typed-blocks.md):Agent Run Action 事件溯源协议(本 RFC 是其 Phase 2 的前置)
- [RFC-0002](0002-agent-team.md):团队事件协议(`UserMessageEvent` / `TeamMessageEvent` 来源)
- AG-UI 协议(`ag_ui` 包):上游 UI 事件标准
- `nexau/archs/llm/llm_aggregators/CLAUDE.md`:Set A 现有实现说明
- `tests/scripts/generate_llm_aggregator_logging_data.py`:现有真实 API 录制脚本
