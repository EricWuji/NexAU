# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Reconstruct a UMP Message from a stream of AG-UI events emitted by Set A.

RFC-0023 §阶段 ①.

Set A (``llm_aggregators/``) emits events in semantic order (text deltas,
tool call starts/args/ends, thinking starts/content/ends). To compare against
Set B's ``finalize()`` ModelResponse dict, we need to fold this event stream
into a canonical ``Message``.

Block ordering rule
-------------------
Set A's text deltas only carry ``message_id`` (not block-level ID). When a
non-text event interrupts text deltas, the current TextBlock is sealed and
subsequent text deltas open a NEW TextBlock. This recovers the block ordering
that Set B preserves via its index-keyed accumulator.

Currently captured (strong equivalence)
---------------------------------------
- TextBlock(text=…)        from TextMessageStartEvent + TextMessageContentEvent
- ReasoningBlock(text=…,
                 signature=…)   from ThinkingTextMessage{Start,Content,End}Event
                                — note: signature is NOT in current AG-UI events,
                                left as None until RFC-0023 §阶段 ② lands the
                                ThinkingTextMessage* extension
- ToolUseBlock(id, name,
              input=parsed JSON)  from ToolCall{Start,Args,End}Event

Currently NOT captured (weak equivalence — the gap)
---------------------------------------------------
- usage / stop_reason / model_name — not emitted by Set A's aggregators today
- Reasoning signature / redacted_data — same

These gaps are exactly what RFC-0023 §阶段 ② aims to close via
``ModelCallFinishedEvent`` and ``ThinkingTextMessage*`` field extension.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from nexau.archs.llm.llm_aggregators.events import (
    Event,
    TextMessageContentEvent,
    TextMessageStartEvent,
    ThinkingTextMessageContentEvent,
    ThinkingTextMessageEndEvent,
    ThinkingTextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from nexau.core.messages import (
    BlockType,
    Message,
    ReasoningBlock,
    Role,
    TextBlock,
    ToolUseBlock,
)

logger = logging.getLogger(__name__)


def reconstruct_message_from_agui(events: list[Event]) -> Message:
    """Fold an AG-UI event stream into a Message.

    Assumes the stream represents exactly one assistant message (one
    TextMessageStartEvent at the head). Streams that span multiple messages
    or that interleave run-lifecycle events outside the message scope should
    be filtered before being passed in.
    """
    role: Role = Role.ASSISTANT
    blocks: list[BlockType] = []

    # Active text accumulator: when set, we are inside a contiguous run of text
    # deltas and the next text delta should append to ``current_text``. A
    # non-text event seals it (flush as TextBlock, reset to None).
    current_text: str | None = None

    # Active reasoning blocks keyed by thinking_message_id. Reasoning blocks
    # are explicitly opened/closed by Start/End events, so we don't need the
    # "implicit close on next event" trick that text uses.
    active_reasoning: dict[str, str] = {}

    # Tool calls under construction, keyed by tool_call_id. ToolCallStart opens,
    # ToolCallArgs accumulates the JSON arguments fragments, ToolCallEnd seals
    # by parsing the buffered JSON and emitting a ToolUseBlock.
    active_tools: dict[str, dict[str, Any]] = {}

    def flush_text() -> None:
        nonlocal current_text
        if current_text is not None:
            blocks.append(TextBlock(text=current_text))
            current_text = None

    for event in events:
        match event:
            case TextMessageStartEvent():
                # The role hint lives here; otherwise this event signals "start
                # of message" and we don't need to allocate anything yet — the
                # first content delta will open a TextBlock.
                if event.role:
                    role = Role(event.role)

            case TextMessageContentEvent():
                if current_text is None:
                    current_text = ""
                current_text += event.delta

            case ThinkingTextMessageStartEvent():
                flush_text()  # seal pending text block before reasoning interrupts
                active_reasoning[event.thinking_message_id] = ""

            case ThinkingTextMessageContentEvent():
                tid = event.thinking_message_id
                if tid not in active_reasoning:
                    logger.warning("ThinkingTextMessageContentEvent for unknown thinking_message_id %s", tid)
                    active_reasoning[tid] = ""
                active_reasoning[tid] += event.delta

            case ThinkingTextMessageEndEvent():
                tid = event.thinking_message_id
                text = active_reasoning.pop(tid, None)
                if text is None:
                    logger.warning("ThinkingTextMessageEndEvent for unknown thinking_message_id %s", tid)
                    continue
                # NOTE: signature/redacted_data not yet on Set A events — RFC-0023 §阶段 ② gap.
                blocks.append(ReasoningBlock(text=text, signature=None, redacted_data=None))

            case ToolCallStartEvent():
                flush_text()
                active_tools[event.tool_call_id] = {
                    "name": event.tool_call_name,
                    "args_buffer": "",
                }

            case ToolCallArgsEvent():
                tid = event.tool_call_id
                if tid not in active_tools:
                    logger.warning("ToolCallArgsEvent for unknown tool_call_id %s", tid)
                    active_tools[tid] = {"name": "", "args_buffer": ""}
                active_tools[tid]["args_buffer"] += event.delta

            case ToolCallEndEvent():
                tid = event.tool_call_id
                tool = active_tools.pop(tid, None)
                if tool is None:
                    logger.warning("ToolCallEndEvent for unknown tool_call_id %s", tid)
                    continue
                args_buffer: str = tool["args_buffer"]
                parsed: dict[str, Any]
                if not args_buffer:
                    parsed = {}
                else:
                    try:
                        parsed = json.loads(args_buffer)
                    except json.JSONDecodeError:
                        # Fall back to "first JSON object" recovery used by
                        # Set B's _finalize_block path.
                        try:
                            first_obj, _ = json.JSONDecoder().raw_decode(args_buffer.lstrip())
                            parsed = first_obj
                        except (json.JSONDecodeError, ValueError):
                            parsed = {"_raw": args_buffer}
                blocks.append(
                    ToolUseBlock(
                        id=tid,
                        name=tool["name"] or "",
                        input=parsed,
                        raw_input=args_buffer if args_buffer else None,
                    )
                )

            case _:
                # Non-content events (Run lifecycle, Compaction, Image, etc.)
                # are ignored at the message level. Run-lifecycle events would
                # be filtered upstream by the harness in §阶段 ②.
                pass

    flush_text()  # seal any trailing text run

    # Final flush: any tools or reasoning still active at end-of-stream are
    # treated as implicitly ended. This mirrors Set B's ``_flush_active_blocks``
    # behavior at finalize() and is needed for truncated streams (e.g. when
    # max_tokens cuts off generation before content_block_stop / message_stop).
    for tool_id, tool in active_tools.items():
        flush_buffer: str = tool["args_buffer"]
        flush_parsed: dict[str, Any]
        if not flush_buffer:
            flush_parsed = {}
        else:
            try:
                flush_parsed = json.loads(flush_buffer)
            except json.JSONDecodeError:
                try:
                    first_obj, _ = json.JSONDecoder().raw_decode(flush_buffer.lstrip())
                    flush_parsed = first_obj
                except (json.JSONDecodeError, ValueError):
                    flush_parsed = {"_raw": flush_buffer}
        blocks.append(
            ToolUseBlock(
                id=tool_id,
                name=tool["name"] or "",
                input=flush_parsed,
                raw_input=flush_buffer if flush_buffer else None,
            )
        )
    active_tools.clear()

    for tid, text in active_reasoning.items():
        blocks.append(ReasoningBlock(text=text, signature=None, redacted_data=None))
    active_reasoning.clear()

    return Message(role=role, content=blocks)
