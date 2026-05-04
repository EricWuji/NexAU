# 2026-05-02 — LLM aggregator parity harness uncovers 3 production drift bugs

**TL;DR**: NexAU has two parallel implementations parsing every provider's
SSE stream — Set A emits Events for live SSE, Set B builds ModelResponse
for persistence. They independently re-parsed the same wire and
**silently drifted in production**: live UI showed one thing, persisted
history showed another. Built a parity harness, recorded 43 real provider
streams across 4 providers, surfaced 3 production drift bugs + 2 test
infrastructure bugs. All five bugs map onto industry-wide LLM streaming
pathologies that other frameworks (vLLM, JetBrains/koog, Spring AI,
LiteLLM) had independently re-discovered the same year.

**Date**: 2026-05-01 to 2026-05-02
**Driver**: PR #508 (RFC-0023 §阶段 ① implementation)
**Files**: `tests/aggregator_parity/`, plus 5 fix commits across the
4 Set A aggregators in `nexau/archs/llm/llm_aggregators/`.

## Initial symptom (and how it stayed hidden)

NexAU's LLM streaming pipeline has two consumers of every raw provider SSE
stream:

```
Provider SSE (raw bytes)
   │
   ├─→ Set A: nexau/archs/llm/llm_aggregators/<provider>/
   │       → emits unified Events → SSE to UI for live display
   │
   └─→ Set B: nexau/archs/main_sub/execution/llm_caller.py *StreamAggregator
           → builds ModelResponse dict → Message → persist to history
```

Both run on every call. Each independently parses the same SSE bytes.
**They had no parity contract** — only matching unit tests against
synthetic events each Set was responsible for. Neither test suite cross-
checked the other's interpretation of the same wire input.

Result: when a provider quirk happened — orphan delta, missing summary,
new extension field — Set A and Set B handled it differently. The user
saw "live SSE" through Set A, then refreshed and saw "persisted history"
through Set B. **Mismatch was visually obvious to users, invisible to
internal tests.**

This pattern had been live for months. The catalyst was RFC-0022 (event
sourcing protocol with iter-level persistence) — Phase 2 requires that
"the SSE-streamed Message" and "the persisted Message" be IDENTICAL at
each iter. Without that, the chunk → aggregate framing leaks at runtime.

## Methodology: parity harness

The pattern that surfaced all five bugs:

1. **Record real provider SSE streams** — not synthetic events constructed
   from SDK types, but the actual byte stream the SDK receives. Use
   `tests/aggregator_parity/scripts/record_fixture.py` (auto-redacts
   `safety_identifier` / `prompt_cache_key`, scans for key leaks).
2. **For each fixture, run BOTH Sets** — feed identical input, capture
   outputs. Set A emits Event stream + reconstruct Message via a generic
   reconstructor. Set B builds dict and convert via `set_b_to_message`.
3. **Compare Messages** — strong assertions on `role` + content blocks
   (count / order / type / primary fields), weak gap recording for fields
   only one Set carries (usage / model / stop_reason — those need Set A
   to emit `ModelCallFinishedEvent` in §阶段 ②).
4. **Strong failure = real production bug** — no skipping with xfail
   unless the divergence is a documented design decision pending RFC
   resolution.

Self-validation tests (`test_meta_self.py`) verify the harness has teeth:
deliberately mutate Set B's output (drop a block, corrupt text) and
assert harness reports drift. Run it as part of the suite — if positive
control fails, the entire suite is meaningless.

## Five bugs caught

### Bug 1 — Anthropic orphan `thinking_delta` (production drift)

**Wire**: `thinking_delta` arrives at index N before any
`content_block_start` (eager streaming pathology).

**Set A**: logged "Received thinking_delta for unknown thinking block"
and dropped the delta. Zero events emitted.

**Set B**: `AnthropicStreamAggregator` inferred the block type from the
delta and created a thinking block on the fly.

**User-visible**: live SSE showed no thinking; reload of persisted
history showed a ReasoningBlock. **Visible drift on session reload.**

**Fix** (d723b002): Mirror Set B's lazy synthesis in
`AnthropicEventAggregator._handle_content_block_delta` — first
thinking_delta with no thinking_id synthesizes UUID + emits
ThinkingTextMessageStartEvent. Same pattern as the existing
`_pending_tool_deltas` / `_flush_pending_with_synthetic` for tool_use.

**OSS prior art**: Spring AI #4407 (2025-09) hit identical pattern in
their Anthropic streaming integration. LiteLLM #25321 (2026-04) hit the
inverse — content_block_start dropping companion deltas.

### Bug 2 — OpenAI Responses silent reasoning (production drift)

**Wire**: gpt-5.x with `reasoning.effort=medium` (or even high +
`summary=detailed`) produces reasoning silently — `output_item.added` for
reasoning has `summary=[]` and no follow-up `reasoning_summary_*` events
ever arrive. Confirmed reproducible across all effort levels.

**Set A**: `_ReasoningItemAggregator` only emitted Start on
`summary_part.added`. With no summary part → no events.

**Set B**: persisted an empty ReasoningBlock as a "reasoning happened"
marker (since `reasoning_tokens` was non-zero in usage).

**User-visible**: live SSE showed no thinking indicator; persisted
history recorded a silent reasoning step.

**Fix** (d723b002): Track `_started`/`_ended` state with idempotent
`_emit_start_if_needed` / `_emit_end_if_needed`. Move Start emission to
the initial `output_item.added` reasoning dispatch. Add `finish()`
called by parent on `output_item.done` to emit End. Silent reasoning
now produces a Start+End pair (empty content) matching Set B's marker.

**OSS prior art**: JetBrains/koog #1264 — same pattern in koog's
`OpenAILLMClient.executeResponsesStreaming`. Multiple OpenAI Developer
Community threads complaining "o3 omits reasoning summary >90% of cases".

### Bug 3 — Gemini block-ordering on thinking → tool transition (production drift)

**Wire**: Gemini wire emits `[reasoning chunk, tool chunk]` in order.

**Set A**: kept the thinking block "open" across the tool emission —
event sequence was Thinking{Start,Content} → Tool{Start,Args,End} →
ThinkingEnd (only at finishReason). Reconstructor closed blocks in
End-event order → `[ToolUseBlock, ReasoningBlock]` instead of wire-correct
`[ReasoningBlock, ToolUseBlock]`.

**Set B**: `GeminiRestStreamAggregator.finalize()` ordered output as
[reasoning, signature, content, tools] regardless of wire order — happens
to match wire here.

**User-visible**: any Gemini response interleaving thinking with text/tool
showed wrong block order in any Start/End-driven downstream consumer.

**Fix** (16288c5c): Added `_close_thinking_if_open()` helper called from
both `_handle_text_part` and `_handle_function_call_part`. Closes
thinking on transition out, preserves wire order downstream.

### Bug 4 — Set B converter wasn't joining `reasoning_details` (test infrastructure)

**Real OpenRouter wire**: `delta.reasoning` (flat str, GPT-OSS style) +
`delta.reasoning_details` (structured array, OpenRouter style). Set A's
`OpenAIChatCompletionAggregator._extract_reasoning_delta` ALREADY pulled
from both fields. Set B's `OpenAIChatStreamAggregator` ALSO preserved
both verbatim.

**The bug was in our parity converter**: `openai_chat_set_b_dict_to_message`
only read `reasoning_content`, ignoring `reasoning_details`. Produced
fewer blocks than Set A's reconstructor → false strong-parity failure.

**Fix** (1270a1b6): Converter now joins `reasoning_content` + concatenated
`reasoning_details[].text` into a single ReasoningBlock.

**OSS prior art**: vLLM RFC-27755 (2025-10) explicitly named this
fragmentation: "DeepSeek originally used `reasoning_content`. OpenAI's
GPT-OSS guidance recommended `reasoning`. vLLM renamed in PR-27752,
kept backwards compat."

### Bug 5 — Gemini tool_id naming (test infrastructure)

Set A's `GeminiRestEventAggregator` synthesized tool IDs as
`f"gemini_tc_{count}"`. Our parity converter used `f"gemini-tool-{count}"`.
Trivial naming mismatch in the test infra; fixed in 16288c5c by aligning
the converter to Set A's convention.

## Reusable methodology

For any future "two parallel implementations of the same parsing logic"
problem in NexAU, the parity-harness recipe:

1. **Record real wire** — synthetic events miss the long tail. Real
   recordings cost ~5 minutes and cost-per-fixture trends to zero with
   a recording script.
2. **Reconstruct to a canonical type** — both Sets must produce the same
   final shape (UMP `Message` here), even via different paths. Compare
   on the canonical type, not on the intermediate event stream.
3. **Strong + weak gap separation** — strong assertions (must hold);
   weak gaps (recorded as informational, drives follow-up). Both go in
   the report; the test fails on strong only.
4. **Self-validation** — write deliberate-mutation tests that prove the
   harness catches drift. If positive control fails, the suite is
   silently passing. Treat this as a load-bearing test.
5. **Cross-validate against OSS** — when you find a divergence, search
   GitHub for the wire pattern. If multiple frameworks hit the same bug,
   it's a class of pathology, not a one-off. Record the prior art in
   the README so future readers know they're not alone.

## Anti-patterns avoided

- **"Both Sets pass their own unit tests, ship it"** — the test suites
  used different synthetic inputs. Each suite independently asserted
  "looks right to me". Neither caught drift.
- **"Just reuse one Set as the test for the other"** — would have caught
  drift but locked in the bug. Need an independent canonical comparison
  point (UMP Message via reconstructor vs converter).
- **"xfail until §阶段 ② handles it"** — fine for design decisions, NOT
  for production drift. The harness initially had 9 xfails; after
  digging into each, 3 were real bugs requiring code fix, 6 were
  obsolete provider routings that no longer exist (deleted).
- **"Coverage % is the goal"** — coverage went from ~25% → ~92%, but the
  signal that mattered was bugs caught, not coverage delta. Some
  remaining ~8% (defensive `raise ValueError on impossible state`) is
  legitimately uncovered; pursuing it has zero test value.

## Sustaining

- Banners on each Set A aggregator file's docstring + a banner above
  the `*StreamAggregator` section in `llm_caller.py` (visible to anyone
  editing the file)
- `CLAUDE.md` has an "Aggregator parity protocol" section (auto-loaded
  context for Claude / agents)
- `tests/aggregator_parity/README.md` is the operations manual
- `tests/aggregator_parity/test_meta_self.py` runs in every CI pass —
  if positive control breaks, the suite is meaningless and CI must fail

After RFC-0023 §阶段 ③ retires Set B (deletes the
`*StreamAggregator` classes), the harness self-degrades into "single-
aggregator regression tests" — still useful for catching provider
protocol changes, but no longer load-bearing for parity. At that point
the banners come down.

## Related work

- RFC-0022: Agent Run Action 事件溯源协议 — defines the event-sourcing
  framing that motivated this work
- RFC-0023: Provider Stream Aggregator Unification — the design RFC
  that made parity testing the official quality gate
- PR #508: implementation of RFC-0023 §阶段 ①
- vLLM RFC-27755 / Spring AI #4407 / JetBrains/koog #1264 / LiteLLM #25321
  — OSS prior art for each of the bug classes
