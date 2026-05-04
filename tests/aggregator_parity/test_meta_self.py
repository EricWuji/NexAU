# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Self-validation tests for the parity harness itself.

These tests answer the meta-question: **does the parity harness actually
have teeth?**

If the parity tests in test_anthropic_parity.py / test_chat_aggregator_parity.py
/ test_responses_aggregator_parity.py all pass, that's only meaningful if
we also know the harness FAILS LOUDLY when given divergent inputs. These
tests exercise that path:

1. **Positive control**: identical inputs to both Sets → ``strong_ok=True``
2. **Negative — Set B mutated**: deliberately corrupt Set B's output (drop
   a block) → harness reports ``strong_ok=False`` with the right message
3. **Negative — Set A mutated**: deliberately corrupt Set A's events (drop
   the closing ToolCallEnd) → harness reports ``strong_ok=False``
3.5. **Vendor-truth structural-comparator teeth**: pin ``compare_structural``
   (axis 3) against deliberately-mutated Messages — block count / tool
   name / tool input keys / tool id format prefix / block type mismatches
   must all be caught. Without this pin the third axis is a silent no-op
   since two-call comparison can't depend on token equality.
4. **Sanity load**: every committed ``.sse`` recording loads without
   error and yields at least one event with a valid ``type`` field

(4) protects against accidentally committing a 0-byte / corrupted /
error-response recording, which would silently skip all parity assertions
on the affected fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.aggregator_parity.fixtures.anthropic import (
    fixture_plain_text as anthropic_synthetic_plain,
)
from tests.aggregator_parity.parity_helpers import (
    anthropic_set_b_dict_to_message,
    run_parity,
)
from tests.aggregator_parity.sse_loader import _parse_sse_blocks  # noqa: PLC2701  internal use OK in self-test


# Use the synthetic plain_text fixture for self-tests — it's deterministic
# and known to be parity-clean.
def _good_events():
    return anthropic_synthetic_plain()


# ============================================================================
# (1) Positive control: identical inputs → strong_ok
# ============================================================================


def test_positive_control_passes() -> None:
    """When both Sets are run normally on the same input, strong parity holds.

    This is the no-op baseline: if THIS fails, the harness is broken in a
    way that makes the entire suite meaningless.
    """
    from tests.aggregator_parity.anthropic_glue import (
        run_set_a_anthropic,
        run_set_b_anthropic,
    )

    events = _good_events()
    report = run_parity(
        fixture_name="self_positive",
        events=events,
        run_set_a=run_set_a_anthropic,
        run_set_b=run_set_b_anthropic,
        set_b_to_message=anthropic_set_b_dict_to_message,
    )
    assert report.strong_ok, f"Positive control failed (harness broken):\n{report}"


# ============================================================================
# (2) Negative: Set B output mutated → harness must catch
# ============================================================================


def test_negative_set_b_dropping_block_is_caught() -> None:
    """Deliberately strip a content block from Set B's output. The harness
    must report strong_ok=False with a 'block count mismatch' message."""
    from tests.aggregator_parity.anthropic_glue import (
        run_set_a_anthropic,
        run_set_b_anthropic,
    )

    def run_set_b_buggy(events):
        result = run_set_b_anthropic(events)
        # Drop the last content block to simulate a regression
        if result.get("content"):
            result["content"] = result["content"][:-1]
        return result

    events = _good_events()
    report = run_parity(
        fixture_name="self_negative_drop_block",
        events=events,
        run_set_a=run_set_a_anthropic,
        run_set_b=run_set_b_buggy,
        set_b_to_message=anthropic_set_b_dict_to_message,
    )
    assert not report.strong_ok, (
        "Harness FAILED to detect Set B dropping a block. This means strong-parity assertions are not actually enforcing what they claim."
    )
    assert any("block count mismatch" in f for f in report.strong_failures), (
        f"Harness caught a divergence but the message didn't mention 'block count mismatch'. Got: {report.strong_failures}"
    )


def test_negative_set_b_corrupting_text_is_caught() -> None:
    """Deliberately mutate Set B's text content. Harness must catch."""
    from tests.aggregator_parity.anthropic_glue import (
        run_set_a_anthropic,
        run_set_b_anthropic,
    )

    def run_set_b_buggy(events):
        result = run_set_b_anthropic(events)
        for block in result.get("content", []):
            if block.get("type") == "text" and "text" in block:
                block["text"] = block["text"] + "_MUTATED"
                break
        return result

    events = _good_events()
    report = run_parity(
        fixture_name="self_negative_corrupt_text",
        events=events,
        run_set_a=run_set_a_anthropic,
        run_set_b=run_set_b_buggy,
        set_b_to_message=anthropic_set_b_dict_to_message,
    )
    assert not report.strong_ok, "Harness FAILED to detect Set B corrupting text content."


# ============================================================================
# (3) Negative: Set A events mutated → harness must catch
# ============================================================================


def test_negative_set_a_truncating_events_is_caught() -> None:
    """Truncate Set A's event stream so the reconstructor produces an
    incomplete Message. Harness must catch."""
    from tests.aggregator_parity.anthropic_glue import (
        run_set_a_anthropic,
        run_set_b_anthropic,
    )

    def run_set_a_buggy(events):
        all_events = run_set_a_anthropic(events)
        # Keep only the first event (TextMessageStartEvent) so all content
        # events are dropped — guaranteed to mutate the reconstructed Message
        # regardless of how many trailing metadata/lifecycle events exist.
        return all_events[:1]

    events = _good_events()
    report = run_parity(
        fixture_name="self_negative_truncate_set_a",
        events=events,
        run_set_a=run_set_a_buggy,
        run_set_b=run_set_b_anthropic,
        set_b_to_message=anthropic_set_b_dict_to_message,
    )
    assert not report.strong_ok, "Harness FAILED to detect Set A truncated events."


# ============================================================================
# (3.5) Negative controls for the VENDOR-TRUTH axis (compare_structural)
# ============================================================================
#
# The third axis (Set A vs vendor non-stream JSON) uses ``compare_structural``
# instead of ``compare_strong`` because the two recordings come from two
# independent LLM calls and token equality is meaningless. But that loose
# yardstick is only useful if it still catches the kinds of divergence the
# axis is meant to detect — block count mismatch, tool name drift, tool
# input schema drift. These tests pin ``compare_structural`` against
# deliberately-mutated Messages.


def _msg(*blocks):
    from nexau.core.messages import Message, Role

    return Message(role=Role.ASSISTANT, content=list(blocks))


def test_compare_structural_positive_baseline() -> None:
    """Same blocks, different text content (mimics two independent LLM calls):
    must NOT fail (that's the whole point of structural comparison)."""
    from nexau.core.messages import TextBlock
    from tests.aggregator_parity.parity_helpers import compare_structural

    a = _msg(TextBlock(text="Stream call generated this text."))
    b = _msg(TextBlock(text="Non-stream call generated different text."))
    assert compare_structural(a, b) == [], "Positive control failed: structural comparison should not depend on text content"


def test_compare_structural_block_count_mismatch_caught() -> None:
    """Stream produces 2 blocks, non-stream produces 1: must be flagged.

    This is the actual server_tool_use class of bug we want to catch — when a
    provider's stream aggregator splits or merges blocks differently than the
    vendor's own aggregation, replaying the Message breaks prompt cache."""
    from nexau.core.messages import TextBlock
    from tests.aggregator_parity.parity_helpers import compare_structural

    a = _msg(TextBlock(text="part one"), TextBlock(text="part two"))
    b = _msg(TextBlock(text="all in one block"))
    failures = compare_structural(a, b)
    assert any("block count mismatch" in f for f in failures), f"Expected block count mismatch, got: {failures}"


def test_compare_structural_tool_name_mismatch_caught() -> None:
    """Different tool names must be flagged (prompt forces a specific tool, so
    a stream/non-stream divergence here is real protocol drift)."""
    from nexau.core.messages import ToolUseBlock
    from tests.aggregator_parity.parity_helpers import compare_structural

    a = _msg(ToolUseBlock(id="toolu_01", name="get_weather", input={"location": "Tokyo"}))
    b = _msg(ToolUseBlock(id="toolu_01", name="get_weather_v2", input={"location": "Tokyo"}))
    failures = compare_structural(a, b)
    assert any("ToolUseBlock.name mismatch" in f for f in failures), f"Expected tool name mismatch, got: {failures}"


def test_compare_structural_tool_input_keys_mismatch_caught() -> None:
    """Stream tool with input={'location'}, non-stream with input={'city'}:
    same tool, different schema — Set A is parsing the wire format wrong."""
    from nexau.core.messages import ToolUseBlock
    from tests.aggregator_parity.parity_helpers import compare_structural

    a = _msg(ToolUseBlock(id="toolu_01", name="get_weather", input={"location": "Tokyo"}))
    b = _msg(ToolUseBlock(id="toolu_01", name="get_weather", input={"city": "Tokyo"}))
    failures = compare_structural(a, b)
    assert any("input top-level keys mismatch" in f for f in failures), f"Expected input keys mismatch, got: {failures}"


def test_compare_structural_tool_id_prefix_mismatch_caught() -> None:
    """Stream uses 'toolu_*' (Anthropic native), non-stream uses 'call_*'
    (OpenAI native): provider format drift between the two paths."""
    from nexau.core.messages import ToolUseBlock
    from tests.aggregator_parity.parity_helpers import compare_structural

    a = _msg(ToolUseBlock(id="toolu_01abc", name="get_weather", input={"location": "Tokyo"}))
    b = _msg(ToolUseBlock(id="call_01abc", name="get_weather", input={"location": "Tokyo"}))
    failures = compare_structural(a, b)
    assert any("id format prefix mismatch" in f for f in failures), f"Expected id prefix mismatch, got: {failures}"


def test_compare_structural_block_type_mismatch_caught() -> None:
    """Stream produces a TextBlock at position 0, non-stream a ToolUseBlock:
    the block lineup differs. Catches reordering / wrong-aggregation bugs."""
    from nexau.core.messages import TextBlock, ToolUseBlock
    from tests.aggregator_parity.parity_helpers import compare_structural

    a = _msg(TextBlock(text="a"))
    b = _msg(ToolUseBlock(id="toolu_01", name="get_weather", input={"x": 1}))
    failures = compare_structural(a, b)
    assert any("block count mismatch" in f or "type mismatch" in f for f in failures), f"Expected count or type mismatch, got: {failures}"


# ============================================================================
# (4) Sanity: every committed recording loads cleanly
# ============================================================================


_RECORDINGS_ROOT = Path(__file__).resolve().parent / "fixtures"

# Provider directory → short ID prefix that DOES NOT contain "openai" / "chat" /
# "llm" — those substrings trigger conftest.pytest_collection_modifyitems
# auto-marking the test ``@pytest.mark.llm`` which then gets skipped without
# a real API key. Same workaround as test_chat_aggregator_parity's filename.
_PROVIDER_ABBREV = {
    "anthropic": "ant",
    "openai_chat": "ocomp",
    "openai_responses": "oresp",
    "gemini_rest": "gem",
}


def _all_recordings() -> list[tuple[str, Path]]:
    return sorted((f"{_PROVIDER_ABBREV[p.parent.parent.name]}_{p.stem}", p) for p in _RECORDINGS_ROOT.glob("**/recordings/*.sse"))


@pytest.mark.parametrize(
    "name,path",
    _all_recordings(),
    ids=[name for name, _ in _all_recordings()],
)
def test_recording_loads_cleanly(name: str, path: Path) -> None:
    """Every .sse recording must:

    - be non-empty
    - parse without raising
    - contain at least one event with a recognizable ``type`` field
      (for OpenAI Chat the marker is ``object: chat.completion.chunk``)
    - NOT be an error response (first JSON line shouldn't have an ``error`` key)

    Catches accidentally-committed broken / empty / error-response fixtures
    that would otherwise silently skip parity assertions.
    """
    raw = path.read_text(encoding="utf-8").strip()
    assert raw, f"{name}: recording is empty"

    # Reject error-response recordings
    first_line = raw.splitlines()[0]
    assert not (first_line.startswith("{") and '"error"' in first_line[:200]), (
        f"{name}: recording is an error response, not an SSE stream:\n  {first_line[:200]}"
    )

    events = _parse_sse_blocks(raw)
    assert events, f"{name}: recording parsed to zero events"

    # Each provider has its own marker field. We accept any of:
    # - ``type`` (Anthropic + OpenAI Responses)
    # - ``object`` ending in 'chunk' (OpenAI Chat Completions)
    # - ``candidates`` (Gemini REST — chunks are partial GenerateContent responses)
    valid = sum(
        1
        for e in events
        if isinstance(e, dict)
        and (
            isinstance(e.get("type"), str)
            or (isinstance(e.get("object"), str) and "chunk" in e["object"])
            or isinstance(e.get("candidates"), list)
        )
    )
    assert valid > 0, f"{name}: no events have a recognizable 'type' / 'object' / 'candidates' field"


# ============================================================================
# (5) gap_report.md freshness — committed report must reflect current state
# ============================================================================
#
# gap_report.md is the input list for RFC-0023 §阶段 ②. If it goes stale
# silently, §阶段 ② plans against an outdated picture of the gaps. This
# test regenerates the body (skipping the timestamp line) and diffs it
# against the committed file. Any drift fails the test with a hint to
# rerun gen_gap_report.py.


def _strip_timestamp(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.startswith("_Generated:"))


def test_gap_report_is_fresh() -> None:
    """The committed gap_report.md must equal the live generator output.

    Catches the failure mode "someone added a fixture / fixed a divergence
    but forgot to regenerate gap_report.md". The timestamp line is excluded
    from the comparison so reruns don't churn diffs.
    """
    from tests.aggregator_parity.scripts import gen_gap_report

    committed_path = Path(__file__).resolve().parent / "gap_report.md"
    if not committed_path.is_file():
        pytest.fail("gap_report.md missing — run: uv run tests/aggregator_parity/scripts/gen_gap_report.py")

    strong_rows = gen_gap_report._run_strong_axis()  # noqa: SLF001
    vendor_rows = gen_gap_report._run_vendor_truth_axis()  # noqa: SLF001
    known = gen_gap_report._load_known_divergent_per_provider()  # noqa: SLF001
    live = gen_gap_report._render(strong_rows, vendor_rows, known)  # noqa: SLF001

    committed = committed_path.read_text(encoding="utf-8")
    if _strip_timestamp(committed) != _strip_timestamp(live):
        pytest.fail(
            "gap_report.md is stale. Regenerate with:\n  uv run tests/aggregator_parity/scripts/gen_gap_report.py\nand commit the result."
        )


# ============================================================================
# (6) KNOWN_* registry orphan-entry guard
# ============================================================================
#
# KNOWN_DIVERGENT_FIXTURES (per-provider) and KNOWN_VENDOR_TRUTH_DIVERGENCES
# are strict-xfail registries. A registry entry pointing to a deleted /
# renamed fixture would silently lose its protection: pytest can't apply
# applymarker(xfail) to a test that no longer exists, and the entry just
# becomes dead config. These tests catch that.


def test_known_divergent_fixtures_have_matching_fixtures() -> None:
    """Every KNOWN_DIVERGENT_FIXTURES entry must name a fixture that exists
    in the corresponding provider's *_FIXTURES list. Otherwise the strict
    xfail is dead config — silent removal of safety net."""
    from tests.aggregator_parity.fixtures.anthropic import ANTHROPIC_FIXTURES
    from tests.aggregator_parity.fixtures.gemini_rest import GEMINI_REST_FIXTURES
    from tests.aggregator_parity.fixtures.openai_chat import OPENAI_CHAT_FIXTURES
    from tests.aggregator_parity.fixtures.openai_responses import OPENAI_RESPONSES_FIXTURES
    from tests.aggregator_parity.test_anthropic_parity import (
        KNOWN_DIVERGENT_FIXTURES as ANT_KD,
    )
    from tests.aggregator_parity.test_chat_aggregator_parity import (
        KNOWN_DIVERGENT_FIXTURES as OAC_KD,
    )
    from tests.aggregator_parity.test_gemini_aggregator_parity import (
        KNOWN_DIVERGENT_FIXTURES as GEM_KD,
    )
    from tests.aggregator_parity.test_responses_aggregator_parity import (
        KNOWN_DIVERGENT_FIXTURES as ORESP_KD,
    )

    suites = [
        ("anthropic", ANT_KD, {n for n, _ in ANTHROPIC_FIXTURES}),
        ("openai_chat", OAC_KD, {n for n, _ in OPENAI_CHAT_FIXTURES}),
        ("openai_responses", ORESP_KD, {n for n, _ in OPENAI_RESPONSES_FIXTURES}),
        ("gemini_rest", GEM_KD, {n for n, _ in GEMINI_REST_FIXTURES}),
    ]

    orphans: list[str] = []
    for provider, kd, fixtures in suites:
        for name in kd:
            if name not in fixtures:
                orphans.append(f"  - {provider}: KNOWN_DIVERGENT_FIXTURES has '{name}' but no such entry in {provider.upper()}_FIXTURES")
    assert not orphans, "Orphan KNOWN_DIVERGENT_FIXTURES entries (strict xfail dead config):\n" + "\n".join(orphans)


def test_known_vendor_truth_divergences_have_matching_pairs() -> None:
    """Every KNOWN_VENDOR_TRUTH_DIVERGENCES entry must name an existing
    `<scenario>.sse` + `<scenario>.non_stream.json` pair. Otherwise the
    strict xfail never fires (no test parameter to apply it to)."""
    from tests.aggregator_parity.test_stream_vs_non_stream import (
        KNOWN_VENDOR_TRUTH_DIVERGENCES,
    )

    orphans: list[str] = []
    for (provider, scenario), _reason in KNOWN_VENDOR_TRUTH_DIVERGENCES.items():
        rec_dir = _RECORDINGS_ROOT / provider / "recordings"
        sse = rec_dir / f"{scenario}.sse"
        non_stream = rec_dir / f"{scenario}.non_stream.json"
        if not sse.is_file() or not non_stream.is_file():
            orphans.append(f"  - ({provider}, {scenario}): missing sse={sse.is_file()} non_stream={non_stream.is_file()}")
    assert not orphans, "Orphan KNOWN_VENDOR_TRUTH_DIVERGENCES entries:\n" + "\n".join(orphans)
