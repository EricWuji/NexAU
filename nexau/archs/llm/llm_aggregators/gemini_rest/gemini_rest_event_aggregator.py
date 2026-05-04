"""Gemini REST event aggregator for streaming chunks.

RFC-0003: Gemini REST 流式事件聚合器

Processes Gemini REST API streaming chunks (plain dicts) and emits
unified START → CONTENT → END events for the transport layer.

⚠️ PARITY PROTOCOL: This module has a twin in
``nexau/archs/main_sub/execution/llm_caller.py`` (``GeminiRestStreamAggregator``)
that MUST stay in lock-step until RFC-0023 §阶段 ③ retires the twin.
Any change to this module's parsing or emission logic requires:

1. Run ``uv run pytest tests/aggregator_parity/`` before commit.
2. If your change handles a new wire pattern (new part type / new
   thoughtSignature shape / new function call wire format), record a
   fixture via ``tests/aggregator_parity/scripts/record_fixture.py``.
3. If parity surfaces a divergence, fix the buggy side rather than xfail
   — real Set A↔Set B drift = real production bug. The harness has
   already caught a block-ordering bug here (thinking → tool transition
   produced wrong block order — fixed in 16288c5c via
   ``_close_thinking_if_open``).

See ``tests/aggregator_parity/README.md`` for the full protocol.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import cast

from ..events import (
    Event,
    ModelCallFinishedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ThinkingTextMessageContentEvent,
    ThinkingTextMessageEndEvent,
    ThinkingTextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)


class GeminiRestEventAggregator:
    """Aggregates Gemini REST streaming dict chunks and emits unified events.

    RFC-0003: Gemini REST 流式事件聚合器

    Each SSE chunk from Gemini streamGenerateContent is a dict with the same
    structure as a non-streaming generateContent response.  This aggregator
    inspects each chunk's parts and emits the appropriate lifecycle events
    (START → CONTENT → END) for text, thinking, and tool calls.
    """

    def __init__(self, *, on_event: Callable[[Event], None], run_id: str) -> None:
        self._on_event = on_event
        self._run_id = run_id
        self._message_id = f"gemini-{uuid.uuid4().hex[:12]}"
        self._thinking_message_id = f"thinking-{uuid.uuid4().hex[:12]}"
        self._started = False
        self._text_started = False
        self._thinking_started = False
        self._thinking_ended = False
        self._tool_call_count = 0
        # Per-call metadata accumulated across chunks (RFC-0023 §阶段 ②)
        self._model_name: str | None = None
        self._model_call_id: str | None = None
        self._finish_reason: str | None = None
        self._usage: dict[str, object] | None = None
        # Thinking signature stored per chunk; attached to End event.
        self._thought_signature: str | None = None
        # Whether ModelCallFinishedEvent has fired (idempotent guard — _handle_finish
        # may run multiple times if chunks repeat finishReason).
        self._metadata_emitted = False

    def aggregate(self, chunk: dict[str, object]) -> None:
        """Process a single Gemini REST streaming chunk and emit events.

        RFC-0003: 处理单个 Gemini 流式数据块并发射事件

        Args:
            chunk: Parsed JSON dict from a Gemini SSE data line
        """
        # 0. Accumulate cross-chunk metadata (RFC-0023 §阶段 ②)
        if isinstance(chunk.get("modelVersion"), str):
            self._model_name = cast(str, chunk["modelVersion"])
        if isinstance(chunk.get("responseId"), str):
            self._model_call_id = cast(str, chunk["responseId"])
        usage_md = chunk.get("usageMetadata")
        if isinstance(usage_md, dict):
            self._usage = cast(dict[str, object], usage_md)

        # 1. 提取 candidates
        candidates_raw = chunk.get("candidates")
        if not isinstance(candidates_raw, list) or not candidates_raw:
            return

        candidates_list = cast(list[object], candidates_raw)
        candidate_obj = candidates_list[0]
        if not isinstance(candidate_obj, dict):
            return
        candidate_dict = cast(dict[str, object], candidate_obj)

        content_obj = candidate_dict.get("content")
        if not isinstance(content_obj, dict):
            # finishReason 可能在没有 content 的 chunk 中
            finish_reason = candidate_dict.get("finishReason")
            if isinstance(finish_reason, str) and finish_reason:
                self._finish_reason = finish_reason
                self._handle_finish()
            return
        content_dict = cast(dict[str, object], content_obj)

        parts = content_dict.get("parts")
        if not isinstance(parts, list):
            return

        # 2. 遍历 parts，分类处理并发射事件
        for part_obj in cast(list[object], parts):
            if not isinstance(part_obj, dict):
                continue
            part_dict = cast(dict[str, object], part_obj)

            # Gemini sometimes emits a part containing ONLY thoughtSignature
            # (no text / no functionCall) as a sibling to a thinking text part.
            # Capture it for the ThinkingTextMessageEndEvent regardless of
            # whether the part also carries content.
            sig = part_dict.get("thoughtSignature")
            if isinstance(sig, str) and sig:
                self._thought_signature = sig

            is_thought = part_dict.get("thought") is True
            has_text = "text" in part_dict
            has_function_call = "functionCall" in part_dict

            if is_thought and has_text:
                self._handle_thinking_part(part_dict)
            elif has_text and not is_thought:
                self._handle_text_part(part_dict)
            elif has_function_call:
                self._handle_function_call_part(part_dict)

        # 3. 检查 finish reason
        finish_reason = candidate_dict.get("finishReason")
        if isinstance(finish_reason, str) and finish_reason:
            self._handle_finish()

    def clear(self) -> None:
        """Reset aggregator state for reuse.

        RFC-0003: 重置聚合器状态以便复用
        """
        self._message_id = f"gemini-{uuid.uuid4().hex[:12]}"
        self._thinking_message_id = f"thinking-{uuid.uuid4().hex[:12]}"
        self._started = False
        self._text_started = False
        self._thinking_started = False
        self._thinking_ended = False
        self._tool_call_count = 0
        self._model_name = None
        self._model_call_id = None
        self._finish_reason = None
        self._usage = None
        self._thought_signature = None
        self._metadata_emitted = False

    def _ensure_message_started(self) -> None:
        """Emit TextMessageStartEvent on first content of any kind."""
        if not self._started:
            self._started = True
            self._on_event(
                TextMessageStartEvent(
                    message_id=self._message_id,
                    role="assistant",
                    timestamp=int(datetime.now().timestamp() * 1000),
                    run_id=self._run_id,
                )
            )

    def _close_thinking_if_open(self) -> None:
        """Emit ThinkingTextMessageEnd if a thinking block is open.

        Called when transitioning from a thinking part to a non-thinking
        part (text or tool call) within the same response. Without this,
        block ordering downstream (e.g. via the parity-test reconstructor
        or any other Start/End-driven aggregator) produces blocks in
        End-event order rather than wire order — putting tool/text BEFORE
        reasoning when the wire ordered them reasoning-then-tool/text.
        """
        if self._thinking_started and not self._thinking_ended:
            self._thinking_ended = True
            self._on_event(
                ThinkingTextMessageEndEvent(
                    timestamp=int(datetime.now().timestamp() * 1000),
                    thinking_message_id=self._thinking_message_id,
                    signature=self._thought_signature,
                )
            )

    def _handle_text_part(self, part: dict[str, object]) -> None:
        """Emit text content events for a non-thinking text part."""
        text = part.get("text")
        if not isinstance(text, str) or not text:
            return

        self._ensure_message_started()
        # Close any open thinking block first so block ordering is preserved
        # downstream (wire order: thinking → text means End thinking before
        # opening text).
        self._close_thinking_if_open()
        self._text_started = True

        self._on_event(
            TextMessageContentEvent(
                message_id=self._message_id,
                delta=text,
                timestamp=int(datetime.now().timestamp() * 1000),
            )
        )

    def _handle_thinking_part(self, part: dict[str, object]) -> None:
        """Emit thinking content events for a thought=true text part."""
        # Gemini may emit thoughtSignature on the same part or a sibling part;
        # capture whenever seen and attach to the End event.
        sig = part.get("thoughtSignature")
        if isinstance(sig, str) and sig:
            self._thought_signature = sig
        text = part.get("text")
        if not isinstance(text, str) or not text:
            return

        self._ensure_message_started()

        if not self._thinking_started:
            self._thinking_started = True
            self._on_event(
                ThinkingTextMessageStartEvent(
                    timestamp=int(datetime.now().timestamp() * 1000),
                    parent_message_id=self._message_id,
                    thinking_message_id=self._thinking_message_id,
                    run_id=self._run_id,
                )
            )

        self._on_event(
            ThinkingTextMessageContentEvent(
                delta=text,
                timestamp=int(datetime.now().timestamp() * 1000),
                thinking_message_id=self._thinking_message_id,
            )
        )

    def _handle_function_call_part(self, part: dict[str, object]) -> None:
        """Emit tool call events for a functionCall part.

        Gemini sends complete function calls (not deltas), so we emit
        START + ARGS + END in sequence for each function call.
        """
        fc = part.get("functionCall")
        if not isinstance(fc, dict):
            return
        fc_dict = cast(dict[str, object], fc)

        name = fc_dict.get("name")
        if not isinstance(name, str):
            return

        args_raw = fc_dict.get("args", {})
        args = cast(dict[str, object], args_raw) if isinstance(args_raw, dict) else {}

        self._ensure_message_started()
        # Close any open thinking block first so the tool block ordering
        # downstream matches wire order (thinking → tool, not tool → thinking).
        self._close_thinking_if_open()

        tool_call_id = f"gemini_tc_{self._tool_call_count}"
        self._tool_call_count += 1

        # START
        self._on_event(
            ToolCallStartEvent(
                tool_call_id=tool_call_id,
                tool_call_name=name,
                parent_message_id=self._message_id,
                timestamp=int(datetime.now().timestamp() * 1000),
            )
        )

        # ARGS (complete JSON since Gemini sends full function calls)
        args_str = json.dumps(args, ensure_ascii=False)
        self._on_event(
            ToolCallArgsEvent(
                tool_call_id=tool_call_id,
                delta=args_str,
                timestamp=int(datetime.now().timestamp() * 1000),
            )
        )

        # END
        self._on_event(
            ToolCallEndEvent(
                tool_call_id=tool_call_id,
                timestamp=int(datetime.now().timestamp() * 1000),
            )
        )

    def _handle_finish(self) -> None:
        """Emit end events when the stream finishes."""
        # 1. 结束 thinking（如果已开始且未结束）
        if self._thinking_started and not self._thinking_ended:
            self._thinking_ended = True
            self._on_event(
                ThinkingTextMessageEndEvent(
                    timestamp=int(datetime.now().timestamp() * 1000),
                    thinking_message_id=self._thinking_message_id,
                    signature=self._thought_signature,
                )
            )

        # 2. 结束 message
        if self._started:
            self._on_event(
                TextMessageEndEvent(
                    message_id=self._message_id,
                    timestamp=int(datetime.now().timestamp() * 1000),
                )
            )

        # 3. RFC-0023 §阶段 ② — emit per-call metadata exactly once.
        # Token usage owned by UsageUpdateEvent (canonical TokenUsage).
        if not self._metadata_emitted:
            self._metadata_emitted = True
            self._on_event(
                ModelCallFinishedEvent(
                    run_id=self._run_id,
                    message_id=self._message_id,
                    model_name=self._model_name,
                    model_call_id=self._model_call_id,
                    stop_reason=self._finish_reason,
                    timestamp=int(datetime.now().timestamp() * 1000),
                )
            )
