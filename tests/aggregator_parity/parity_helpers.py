# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Helpers for running both aggregators and comparing their outputs.

RFC-0023 §阶段 ①.

Three-axis equivalence (all three must be green to retire Set B in §阶段 ③):

- **Strong** (Set A vs Set B): role + content blocks (count, order, type,
  primary fields, including text content). Both sides consume the same
  input, so byte equality is the right yardstick. Failures = real drift.
  See ``compare_strong``.

- **Weak gaps** (Set A vs Set B): fields that Set A's events don't carry
  today (usage, stop_reason, model_name, reasoning signature,
  redacted_data). Recorded — not asserted — as the input list for
  RFC-0023 §阶段 ②. See ``collect_weak_gaps``.

- **Vendor truth** (Set A vs vendor non-stream JSON): protects against the
  "both sides agree but both are wrong" failure mode. The risk is
  prompt-cache-prefix breakage when the aggregated Message is replayed
  back to the vendor next turn. Compares **structurally** (block count /
  order / type + tool name + input top-level keys + id format prefix),
  NOT byte-equal — two independent LLM calls can't produce the same
  tokens. See ``compare_structural``; teeth pinned in
  ``test_meta_self.py``.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Iterable
from typing import Any

from nexau.archs.llm.llm_aggregators.events import Event
from nexau.core.messages import (
    BlockType,
    Message,
    ReasoningBlock,
    Role,
    TextBlock,
    ToolUseBlock,
)

# ============================================================================
# Set B → Message conversion
# ============================================================================


def gemini_set_b_dict_to_message(payload: dict[str, Any]) -> Message:
    """Convert ``GeminiRestStreamAggregator.finalize()`` output to a Message.

    Set B's Gemini finalize() returns a dict that matches the non-streaming
    ``generateContent`` response shape:
        {
            "candidates": [{
                "content": {
                    "parts": [
                        {"text": "...", "thought": true},     # reasoning (optional)
                        {"thoughtSignature": "..."},           # reasoning signature (optional)
                        {"text": "..."},                        # content text (optional)
                        {"functionCall": {"name", "args"}},    # tool call(s) (optional)
                    ],
                    "role": "model",
                },
            }],
            "usageMetadata": {...},
        }

    Mapping to UMP:
    - thought-text part → ReasoningBlock (signature pulled from
      thoughtSignature if present alongside)
    - text part (no thought=true) → TextBlock
    - functionCall part → ToolUseBlock with id derived from name + index
      since Gemini doesn't return tool_call_id natively
    """
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return Message(role=Role.ASSISTANT, content=[])

    candidate = candidates[0]
    content = candidate.get("content") if isinstance(candidate, dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return Message(role=Role.ASSISTANT, content=[])

    blocks: list[BlockType] = []
    thought_signature: str | None = None
    tool_call_index = 0

    # First pass: capture thoughtSignature (often emitted as its own part)
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("thoughtSignature"), str):
            thought_signature = part["thoughtSignature"]

    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("thought") is True and isinstance(part.get("text"), str):
            blocks.append(
                ReasoningBlock(
                    text=part["text"],
                    signature=thought_signature,
                    redacted_data=None,
                )
            )
        elif "text" in part and not part.get("thought") and isinstance(part.get("text"), str):
            blocks.append(TextBlock(text=part["text"]))
        elif isinstance(part.get("functionCall"), dict):
            fc = part["functionCall"]
            args = fc.get("args") or {}
            # Match Set A's GeminiRestEventAggregator naming convention
            # (gemini_rest_event_aggregator.py: f"gemini_tc_{count}").
            # Gemini doesn't expose a tool_call_id natively in its wire
            # format, so both sides have to synthesize — same convention
            # keeps parity comparable.
            tool_id = f"gemini_tc_{tool_call_index}"
            tool_call_index += 1
            blocks.append(
                ToolUseBlock(
                    id=tool_id,
                    name=str(fc.get("name", "")),
                    input=args if isinstance(args, dict) else {},
                    raw_input=None,
                )
            )

    return Message(role=Role.ASSISTANT, content=blocks)


def openai_chat_set_b_dict_to_message(payload: dict[str, Any]) -> Message:
    """Convert ``OpenAIChatStreamAggregator.finalize()`` output to a Message.

    Set B's Chat finalize() returns:
        {
            "role": "assistant",
            "content": "<text>" | [{"type": "output_text", "text": "..."}, ...],
            "tool_calls": [{"id", "type", "function": {"name", "arguments"}}, ...],
            "reasoning_content": "<text>"  # optional, DeepSeek-style flat
            "reasoning_details": [...]      # optional, OpenRouter-style structured
            "model": "...",
            "usage": {...},
        }

    Mapping to UMP:
    - reasoning_content (str) → ReasoningBlock(text=...) prepended
    - content (str|list) → TextBlock concatenated
    - tool_calls → ToolUseBlock per entry (id, name, parsed JSON args)

    Note: Chat Completions does not preserve interleaved block ordering — text
    and tool_calls are flat top-level fields, so reconstruction order is
    reasoning → text → tools.
    """
    blocks: list[BlockType] = []

    # Reasoning first (chronologically: reasoning is emitted before content
    # in real DeepSeek/OpenRouter streams). Two parallel wire formats:
    #   - reasoning_content (str)         — DeepSeek / Qwen / vLLM
    #   - reasoning_details (list[dict])  — OpenRouter
    # Both are preserved verbatim by Set B's OpenAIChatStreamAggregator;
    # the converter joins them so the reconstructed Message has a single
    # ReasoningBlock, matching what Set A's reconstructor produces from
    # ThinkingTextMessage{Start,Content,End} events (Set A's
    # _extract_reasoning_delta also pulls from both fields).
    reasoning_parts: list[str] = []
    reasoning_content = payload.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content:
        reasoning_parts.append(reasoning_content)
    reasoning_details = payload.get("reasoning_details")
    if isinstance(reasoning_details, list):
        for item in reasoning_details:
            if not isinstance(item, dict):
                continue
            # OpenRouter shapes: {type: reasoning.text, text: "..."} or
            # {type: reasoning.summary, summary: "..."}
            text = item.get("text") or item.get("summary") or ""
            if text:
                reasoning_parts.append(str(text))
    if reasoning_parts:
        blocks.append(ReasoningBlock(text="".join(reasoning_parts), signature=None, redacted_data=None))

    # Then text content
    content = payload.get("content")
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        # OpenRouter-style list of typed parts
        text = "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "output_text")
    if text:
        blocks.append(TextBlock(text=text))

    # Then tool calls
    for tc in payload.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args_str = fn.get("arguments", "") or ""
        try:
            args_dict = json.loads(args_str) if args_str else {}
        except json.JSONDecodeError:
            args_dict = {"_raw": args_str}
        blocks.append(
            ToolUseBlock(
                id=str(tc.get("id", "")),
                name=str(fn.get("name", "")),
                input=args_dict if isinstance(args_dict, dict) else {},
                raw_input=args_str if args_str else None,
            )
        )

    return Message(role=Role.ASSISTANT, content=blocks)


def openai_responses_set_b_dict_to_message(payload: dict[str, Any]) -> Message:
    """Convert ``OpenAIResponsesStreamAggregator.finalize()`` output to a Message.

    Set B's Responses finalize() returns:
        {
            "id": "resp_...",
            "model": "...",
            "output": [
                {"type": "message", ..., "content": [
                    {"type": "output_text", "text": "..."}
                ]},
                {"type": "function_call", "id": "...", "call_id": "...",
                 "name": "...", "arguments": "..."},
                {"type": "reasoning", "id": "...", "summary": [
                    {"type": "summary_text", "text": "..."}
                ]},
            ],
            "usage": {...},   # optional
        }

    Mapping to UMP:
    - output[].type=message + content[]=output_text → TextBlock(text=concat)
    - output[].type=function_call → ToolUseBlock(id, name, input=parsed JSON)
    - output[].type=reasoning → ReasoningBlock(text=concat of summary[].text)
    """
    blocks: list[BlockType] = []
    for item in payload.get("output", []):
        item_type = item.get("type")
        if item_type == "message":
            # Concatenate all output_text parts within the message
            text_parts = []
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    text_parts.append(part.get("text", "") or "")
            if text_parts:
                blocks.append(TextBlock(text="".join(text_parts)))
        elif item_type == "function_call":
            args_str = item.get("arguments", "") or ""
            try:
                args_dict = json.loads(args_str) if args_str else {}
            except json.JSONDecodeError:
                args_dict = {"_raw": args_str}
            blocks.append(
                ToolUseBlock(
                    id=str(item.get("call_id") or item.get("id", "")),
                    name=str(item.get("name", "")),
                    input=args_dict if isinstance(args_dict, dict) else {},
                    raw_input=args_str if args_str else None,
                )
            )
        elif item_type == "reasoning":
            summary_parts = []
            for s in item.get("summary", []):
                if s.get("type") == "summary_text":
                    summary_parts.append(s.get("text", "") or "")
            blocks.append(
                ReasoningBlock(
                    text="".join(summary_parts),
                    signature=None,
                    redacted_data=item.get("encrypted_content"),
                )
            )

    return Message(role=Role.ASSISTANT, content=blocks)


def anthropic_set_b_dict_to_message(payload: dict[str, Any]) -> Message:
    """Convert ``AnthropicStreamAggregator.finalize()`` output to a Message.

    Set B finalize() returns:
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "..."},
                {"type": "thinking", "thinking": "...", "signature": "..."},
                {"type": "redacted_thinking", "data": "..."},
                {"type": "tool_use", "id": "...", "name": "...", "input": {...}},
                ...
            ],
            "model": "...",          # optional
            "stop_reason": "...",     # optional
            "usage": {...},           # optional
        }

    This mirrors Anthropic's API response shape, so block ordering is preserved
    (unlike ``ModelResponse.from_anthropic_message`` which flattens text blocks).
    """
    role_str = payload.get("role", "assistant")
    role = Role(role_str) if role_str in {r.value for r in Role} else Role.ASSISTANT

    blocks: list[BlockType] = []
    content = payload.get("content", [])
    for block in content:
        block_type = block.get("type")
        if block_type == "text":
            blocks.append(TextBlock(text=block.get("text", "") or ""))
        elif block_type == "thinking":
            blocks.append(
                ReasoningBlock(
                    text=block.get("thinking", "") or "",
                    signature=block.get("signature"),
                    redacted_data=None,
                )
            )
        elif block_type == "redacted_thinking":
            blocks.append(
                ReasoningBlock(
                    text="",
                    signature=None,
                    redacted_data=block.get("data"),
                )
            )
        elif block_type in {"tool_use", "server_tool_use"}:
            input_dict = block.get("input")
            if not isinstance(input_dict, dict):
                input_dict = {}
            blocks.append(
                ToolUseBlock(
                    id=str(block.get("id", "")),
                    name=str(block.get("name", "")),
                    input=input_dict,
                    raw_input=json.dumps(input_dict, ensure_ascii=False) if input_dict else None,
                )
            )
        # Unknown block types: skip (they'd fail strong equivalence anyway)

    return Message(role=role, content=blocks)


# ============================================================================
# Comparison
# ============================================================================


@dataclasses.dataclass
class ParityGap:
    """A single weakly-different field between the two sides."""

    field: str
    set_a_value: Any
    set_b_value: Any
    note: str = ""


@dataclasses.dataclass
class ParityReport:
    """Three-axis report (RFC-0023 §阶段 ①):

    - ``strong_failures`` — Set A vs Set B exact equivalence (token-level).
      Both consume the same input, so byte equality is the right yardstick.
    - ``weak_gaps`` — fields Set B carries today but Set A doesn't, target
      for §阶段 ② to close.
    - ``vendor_truth_failures`` — Set A's reconstructed Message vs the
      vendor's own non-stream JSON response. Two independent LLM calls,
      so this is **structural** (block count/order/type/tool name/input
      keys/id format prefix), not byte-level — see ``compare_structural``.
    """

    fixture: str
    strong_failures: list[str] = dataclasses.field(default_factory=list)
    weak_gaps: list[ParityGap] = dataclasses.field(default_factory=list)
    vendor_truth_failures: list[str] = dataclasses.field(default_factory=list)

    @property
    def strong_ok(self) -> bool:
        return not self.strong_failures

    @property
    def vendor_truth_ok(self) -> bool:
        return not self.vendor_truth_failures

    def __str__(self) -> str:  # pragma: no cover (debug aid)
        lines = [f"ParityReport(fixture={self.fixture!r})"]
        if self.strong_failures:
            lines.append("  STRONG FAILURES:")
            lines.extend(f"    - {msg}" for msg in self.strong_failures)
        if self.weak_gaps:
            lines.append("  WEAK GAPS (expected — closed by RFC-0023 §阶段 ②):")
            for gap in self.weak_gaps:
                lines.append(f"    - {gap.field}: a={gap.set_a_value!r} b={gap.set_b_value!r} ({gap.note})")
        if self.vendor_truth_failures:
            lines.append("  VENDOR TRUTH FAILURES (Set A vs vendor non-stream):")
            lines.extend(f"    - {msg}" for msg in self.vendor_truth_failures)
        if not self.strong_failures and not self.weak_gaps and not self.vendor_truth_failures:
            lines.append("  (fully equivalent)")
        return "\n".join(lines)


def compare_strong(msg_a: Message, msg_b: Message) -> list[str]:
    """Return list of strong-equivalence failures (empty == pass)."""
    failures: list[str] = []

    if msg_a.role != msg_b.role:
        failures.append(f"role mismatch: a={msg_a.role.value} b={msg_b.role.value}")

    if len(msg_a.content) != len(msg_b.content):
        failures.append(
            f"block count mismatch: a={len(msg_a.content)} b={len(msg_b.content)} "
            f"(a_types={[type(b).__name__ for b in msg_a.content]}, "
            f"b_types={[type(b).__name__ for b in msg_b.content]})"
        )
        return failures  # bail; per-block comparison meaningless when counts differ

    for i, (ba, bb) in enumerate(zip(msg_a.content, msg_b.content, strict=True)):
        if type(ba) is not type(bb):
            failures.append(f"block[{i}] type mismatch: a={type(ba).__name__} b={type(bb).__name__}")
            continue

        if isinstance(ba, TextBlock) and isinstance(bb, TextBlock):
            if ba.text != bb.text:
                failures.append(f"block[{i}] TextBlock.text mismatch: a={ba.text!r} b={bb.text!r}")

        elif isinstance(ba, ReasoningBlock) and isinstance(bb, ReasoningBlock):
            if ba.text != bb.text:
                failures.append(f"block[{i}] ReasoningBlock.text mismatch: a={ba.text!r} b={bb.text!r}")
            # signature and redacted_data are weak (Set A doesn't carry them yet)

        elif isinstance(ba, ToolUseBlock) and isinstance(bb, ToolUseBlock):
            if ba.id != bb.id:
                failures.append(f"block[{i}] ToolUseBlock.id mismatch: a={ba.id!r} b={bb.id!r}")
            if ba.name != bb.name:
                failures.append(f"block[{i}] ToolUseBlock.name mismatch: a={ba.name!r} b={bb.name!r}")
            if ba.input != bb.input:
                failures.append(f"block[{i}] ToolUseBlock.input mismatch: a={ba.input!r} b={bb.input!r}")

    return failures


def compare_structural(msg_a: Message, msg_b: Message) -> list[str]:
    """Structural equivalence — block count / types / order, but NOT token content.

    Used by the vendor-truth axis (`test_stream_vs_non_stream.py`) where ``msg_a``
    and ``msg_b`` come from **two independent LLM calls** (one stream, one
    non-stream). LLMs are non-deterministic across calls — even at
    ``temperature=0`` byte equality of generated text isn't guaranteed,
    especially with provider routing on a gateway.

    What we CAN reliably compare across two independent calls is the **shape**
    of the aggregated Message — which is exactly what the prompt-cache-prefix
    risk is about: when we replay an aggregated Message back to the vendor
    next turn, the byte shape (block types, order, tool-call id format,
    tool names, structural keys) must match what the vendor itself would
    have produced on a non-stream call. The actual generated text tokens
    are vendor-side and not what we're verifying.

    Checks:
    - role match
    - block count + per-position type match
    - TextBlock: text is non-empty (presence)
    - ReasoningBlock: text or redacted_data presence on both sides
    - ToolUseBlock: name match + ``input`` is a dict with the SAME top-level
      keys (values may differ — prompts give the model latitude); id non-empty
      and id format prefix matches across both sides (e.g., both ``toolu_*``)
    """
    failures: list[str] = []

    if msg_a.role != msg_b.role:
        failures.append(f"role mismatch: a={msg_a.role.value} b={msg_b.role.value}")

    if len(msg_a.content) != len(msg_b.content):
        failures.append(
            f"block count mismatch: a={len(msg_a.content)} b={len(msg_b.content)} "
            f"(a_types={[type(b).__name__ for b in msg_a.content]}, "
            f"b_types={[type(b).__name__ for b in msg_b.content]})"
        )
        return failures

    for i, (ba, bb) in enumerate(zip(msg_a.content, msg_b.content, strict=True)):
        if type(ba) is not type(bb):
            failures.append(f"block[{i}] type mismatch: a={type(ba).__name__} b={type(bb).__name__}")
            continue

        if isinstance(ba, TextBlock) and isinstance(bb, TextBlock):
            if not ba.text:
                failures.append(f"block[{i}] TextBlock (a) is empty")
            if not bb.text:
                failures.append(f"block[{i}] TextBlock (b) is empty")

        elif isinstance(ba, ReasoningBlock) and isinstance(bb, ReasoningBlock):
            a_has = bool(ba.text or ba.redacted_data)
            b_has = bool(bb.text or bb.redacted_data)
            if a_has != b_has:
                failures.append(f"block[{i}] ReasoningBlock content presence mismatch: a_has={a_has} b_has={b_has}")

        elif isinstance(ba, ToolUseBlock) and isinstance(bb, ToolUseBlock):
            if ba.name != bb.name:
                failures.append(f"block[{i}] ToolUseBlock.name mismatch: a={ba.name!r} b={bb.name!r}")
            a_keys = set(ba.input.keys()) if isinstance(ba.input, dict) else set()
            b_keys = set(bb.input.keys()) if isinstance(bb.input, dict) else set()
            if a_keys != b_keys:
                failures.append(f"block[{i}] ToolUseBlock.input top-level keys mismatch: a={sorted(a_keys)} b={sorted(b_keys)}")
            if not ba.id:
                failures.append(f"block[{i}] ToolUseBlock.id (a) is empty")
            if not bb.id:
                failures.append(f"block[{i}] ToolUseBlock.id (b) is empty")
            if ba.id and bb.id:
                a_prefix = ba.id.split("_", 1)[0] if "_" in ba.id else ba.id[:5]
                b_prefix = bb.id.split("_", 1)[0] if "_" in bb.id else bb.id[:5]
                if a_prefix != b_prefix:
                    failures.append(f"block[{i}] ToolUseBlock.id format prefix mismatch: a={a_prefix!r} b={b_prefix!r}")

    return failures


def collect_weak_gaps(
    *,
    msg_a: Message,
    msg_b: Message,
    set_b_payload: dict[str, Any],
    agui_events: list[Event] | None = None,
) -> list[ParityGap]:
    """Identify fields present in Set B's output that Set A doesn't carry.

    These are the targets for RFC-0023 §阶段 ② event extensions.

    With §阶段 ② landed, Set A emits ``ModelCallFinishedEvent`` carrying
    ``model_name`` / ``stop_reason`` / ``usage`` / ``llm_call_id``. We scan
    ``agui_events`` for that event and only flag a gap if Set B has the
    field set but Set A's event doesn't carry an equivalent. Synthetic
    fixtures that don't surface metadata on either side stay clean.
    """
    gaps: list[ParityGap] = []

    # Pull the per-call metadata event Set A emits at end-of-call (§阶段 ②).
    # There's at most one ModelCallFinishedEvent per LLM call; take the first
    # if duplicated.
    from nexau.archs.llm.llm_aggregators.events import ModelCallFinishedEvent  # local import to avoid cycle

    a_metadata: ModelCallFinishedEvent | None = None
    if agui_events:
        for ev in agui_events:
            if isinstance(ev, ModelCallFinishedEvent):
                a_metadata = ev
                break

    # Set B finalize key → Set A ModelCallFinishedEvent attribute name.
    # ``usage`` is intentionally NOT in this map — it lives on the separate
    # ``UsageUpdateEvent`` (canonical TokenUsage), not on ModelCallFinishedEvent.
    # PR-C.2 will have aggregator emit UsageUpdateEvent itself; until then
    # the middleware emits it from model_response, so axis-2 isn't the right
    # place to track usage anyway.
    set_b_to_set_a_field = {
        "stop_reason": "stop_reason",
        "model": "model_name",
    }
    for set_b_field, set_a_attr in set_b_to_set_a_field.items():
        b_value = set_b_payload.get(set_b_field)
        if b_value is None:
            continue  # Nothing to compare, no gap
        a_value = getattr(a_metadata, set_a_attr, None) if a_metadata else None
        if a_value is None:
            gaps.append(
                ParityGap(
                    field=f"top_level.{set_b_field}",
                    set_a_value=None,
                    set_b_value=b_value,
                    note=(
                        "Set B has it but Set A's ModelCallFinishedEvent doesn't carry it "
                        "(either the event wasn't emitted on this fixture or the field is None). "
                        "Either fix the aggregator to capture this field, or — if the fixture "
                        "genuinely doesn't surface it on the wire — add a synthetic event."
                    ),
                )
            )

    # Per-ReasoningBlock signature / redacted_data — Set A now ships these on
    # ThinkingTextMessageEndEvent. Scan the event stream and only flag a gap
    # if the matching End event lacks what Set B's block carries.
    end_signatures: list[str | None] = []
    end_redacted: list[str | None] = []
    if agui_events:
        from nexau.archs.llm.llm_aggregators.events import ThinkingTextMessageEndEvent  # noqa: PLC0415

        for ev in agui_events:
            if isinstance(ev, ThinkingTextMessageEndEvent):
                end_signatures.append(ev.signature)
                end_redacted.append(ev.redacted_data)

    reasoning_index = 0
    for i, (ba, bb) in enumerate(zip(msg_a.content, msg_b.content, strict=False)):
        if isinstance(ba, ReasoningBlock) and isinstance(bb, ReasoningBlock):
            sig_from_event = end_signatures[reasoning_index] if reasoning_index < len(end_signatures) else None
            red_from_event = end_redacted[reasoning_index] if reasoning_index < len(end_redacted) else None
            reasoning_index += 1

            if bb.signature and not (ba.signature or sig_from_event):
                gaps.append(
                    ParityGap(
                        field=f"block[{i}].ReasoningBlock.signature",
                        set_a_value=None,
                        set_b_value=bb.signature,
                        note="ThinkingTextMessage* event extension target",
                    )
                )
            if bb.redacted_data and not (ba.redacted_data or red_from_event):
                gaps.append(
                    ParityGap(
                        field=f"block[{i}].ReasoningBlock.redacted_data",
                        set_a_value=None,
                        set_b_value=bb.redacted_data,
                        note="ThinkingTextMessage* event extension target",
                    )
                )

    return gaps


# ============================================================================
# Vendor non-stream JSON → Message (RFC-0023 §阶段 ① — vendor truth axis)
# ============================================================================
#
# These converters take the vendor's NON-streaming HTTP response body (as a
# parsed JSON dict) and reduce it to the same UMP Message shape that the
# stream aggregators (Set A / Set B) produce on the matching SSE recording.
#
# Why this matters
# ----------------
# Both aggregators agreeing with each other (Set A vs Set B) is necessary but
# not sufficient. They could agree and BOTH be wrong relative to the vendor's
# canonical aggregation. The risk that motivates this axis is **prompt cache
# hit rate**: when an aggregated assistant Message is replayed back to the
# vendor on the next turn, it must match the byte-shape the vendor itself
# would have produced on a non-stream call — otherwise the prompt cache
# prefix breaks and we silently lose latency / cost.
#
# Each loader below either reuses the matching ``<provider>_set_b_dict_to_message``
# (when the non-stream response shape happens to coincide with what Set B's
# ``finalize()`` returns — true for Anthropic / OpenAI Responses / Gemini) or
# does a thin shape adjustment first (OpenAI Chat needs to dig into
# ``choices[0].message``).


def anthropic_non_stream_json_to_message(payload: dict[str, Any]) -> Message:
    """Anthropic ``/v1/messages`` non-stream response → Message.

    The response shape is identical to ``AnthropicStreamAggregator.finalize()``
    (Set B was deliberately built to mirror Anthropic's API), so we can reuse
    the existing converter directly.
    """
    return anthropic_set_b_dict_to_message(payload)


def openai_chat_non_stream_json_to_message(payload: dict[str, Any]) -> Message:
    """OpenAI ``/v1/chat/completions`` non-stream response → Message.

    Non-stream wraps the message under ``choices[0].message``; Set B's
    finalize() returns just the message dict directly. We unwrap then reuse.
    """
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return Message(role=Role.ASSISTANT, content=[])
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return Message(role=Role.ASSISTANT, content=[])
    return openai_chat_set_b_dict_to_message(message)


def openai_responses_non_stream_json_to_message(payload: dict[str, Any]) -> Message:
    """OpenAI ``/v1/responses`` non-stream response → Message.

    The non-stream and Set B finalize() shapes are both the same Response
    object (``{id, model, output: [...], usage}``).
    """
    return openai_responses_set_b_dict_to_message(payload)


def gemini_non_stream_json_to_message(payload: dict[str, Any]) -> Message:
    """Gemini ``generateContent`` non-stream response → Message.

    Same shape as Set B's GeminiRestStreamAggregator finalize().
    """
    return gemini_set_b_dict_to_message(payload)


# Provider name → non-stream JSON converter. Parametrized tests look this
# up by directory name (matches the fixtures/<provider>/ folder layout).
NON_STREAM_LOADERS: dict[str, Callable[[dict[str, Any]], Message]] = {
    "anthropic": anthropic_non_stream_json_to_message,
    "openai_chat": openai_chat_non_stream_json_to_message,
    "openai_responses": openai_responses_non_stream_json_to_message,
    "gemini_rest": gemini_non_stream_json_to_message,
}


# ============================================================================
# Generic harness — provider-agnostic
# ============================================================================

ProviderEvent = Any  # Anthropic / OpenAI / Gemini SDK type, depending on provider


def run_parity(
    *,
    fixture_name: str,
    events: Iterable[ProviderEvent],
    run_set_a: Callable[[list[ProviderEvent]], list[Event]],
    run_set_b: Callable[[list[ProviderEvent]], dict[str, Any]],
    set_b_to_message: Callable[[dict[str, Any]], Message],
) -> ParityReport:
    """Drive both aggregators on the same input and produce a ParityReport.

    Provider-specific glue (how to feed events into each aggregator and how to
    convert the dict back to Message) is supplied by callbacks.
    """
    from tests.aggregator_parity.reconstructor import reconstruct_message_from_agui

    event_list = list(events)

    agui_events = run_set_a(event_list)
    msg_a = reconstruct_message_from_agui(agui_events)

    set_b_payload = run_set_b(event_list)
    msg_b = set_b_to_message(set_b_payload)

    return ParityReport(
        fixture=fixture_name,
        strong_failures=compare_strong(msg_a, msg_b),
        weak_gaps=collect_weak_gaps(msg_a=msg_a, msg_b=msg_b, set_b_payload=set_b_payload, agui_events=agui_events),
    )
