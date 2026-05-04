#!/usr/bin/env python3
# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Generate ``tests/aggregator_parity/gap_report.md`` from the live harness.

RFC-0023 §阶段 ① writes a gap report that §阶段 ② consumes as the input list
of fields Set A still doesn't carry. This script runs the parity harness
against every registered fixture across all 4 providers, collects per-axis
status, and emits a markdown summary.

Three axes are reported:

1. **Set A vs Set B strong equivalence** — pass / fail / xfail per fixture
2. **Set A vs Set B weak gaps** — fields aggregated across all fixtures
   (target list for RFC-0023 §阶段 ②)
3. **Set A vs vendor non-stream** — only for fixtures that have a paired
   ``<scenario>.non_stream.json`` on disk

Run:

    uv run tests/aggregator_parity/scripts/gen_gap_report.py

Idempotent: overwrites ``gap_report.md`` each run.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
# When run as a standalone script (not via pytest), the repo root isn't on
# sys.path. Insert it so ``import tests.aggregator_parity...`` resolves.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
PARITY_DIR = REPO_ROOT / "tests" / "aggregator_parity"
FIXTURES_ROOT = PARITY_DIR / "fixtures"
OUTPUT_PATH = PARITY_DIR / "gap_report.md"


def _load_known_divergent_per_provider() -> dict[str, set[str]]:
    """Return {provider: {fixture_name, ...}} of fixtures that have an
    accepted strict-xfail in their test files. The gap report cross-refs
    these so a registered design-discussion divergence doesn't masquerade
    as a hard regression in the strong-axis section.
    """
    out: dict[str, set[str]] = {}
    for provider, module in [
        ("anthropic", "tests.aggregator_parity.test_anthropic_parity"),
        ("openai_chat", "tests.aggregator_parity.test_chat_aggregator_parity"),
        ("openai_responses", "tests.aggregator_parity.test_responses_aggregator_parity"),
        ("gemini_rest", "tests.aggregator_parity.test_gemini_aggregator_parity"),
    ]:
        try:
            mod = __import__(module, fromlist=["KNOWN_DIVERGENT_FIXTURES"])
            out[provider] = set(getattr(mod, "KNOWN_DIVERGENT_FIXTURES", {}))
        except ImportError:
            out[provider] = set()
    return out


def _run_strong_axis():
    """Return list of (provider, fixture_name, strong_failures, weak_gaps)."""
    rows = []

    from tests.aggregator_parity.anthropic_glue import (
        run_set_a_anthropic,
        run_set_b_anthropic,
    )
    from tests.aggregator_parity.fixtures.anthropic import ANTHROPIC_FIXTURES
    from tests.aggregator_parity.fixtures.gemini_rest import GEMINI_REST_FIXTURES
    from tests.aggregator_parity.fixtures.openai_chat import OPENAI_CHAT_FIXTURES
    from tests.aggregator_parity.fixtures.openai_responses import OPENAI_RESPONSES_FIXTURES
    from tests.aggregator_parity.gemini_glue import run_set_a_gemini, run_set_b_gemini
    from tests.aggregator_parity.openai_chat_glue import (
        run_set_a_openai_chat,
        run_set_b_openai_chat,
    )
    from tests.aggregator_parity.openai_responses_glue import (
        run_set_a_openai_responses_events_only,
        run_set_b_openai_responses,
    )
    from tests.aggregator_parity.parity_helpers import (
        anthropic_set_b_dict_to_message,
        gemini_set_b_dict_to_message,
        openai_chat_set_b_dict_to_message,
        openai_responses_set_b_dict_to_message,
        run_parity,
    )

    suites = [
        ("anthropic", ANTHROPIC_FIXTURES, run_set_a_anthropic, run_set_b_anthropic, anthropic_set_b_dict_to_message),
        ("openai_chat", OPENAI_CHAT_FIXTURES, run_set_a_openai_chat, run_set_b_openai_chat, openai_chat_set_b_dict_to_message),
        (
            "openai_responses",
            OPENAI_RESPONSES_FIXTURES,
            run_set_a_openai_responses_events_only,
            run_set_b_openai_responses,
            openai_responses_set_b_dict_to_message,
        ),
        ("gemini_rest", GEMINI_REST_FIXTURES, run_set_a_gemini, run_set_b_gemini, gemini_set_b_dict_to_message),
    ]

    for provider, fixtures, run_a, run_b, to_msg in suites:
        for name, fn in fixtures:
            try:
                events = fn()
                report = run_parity(
                    fixture_name=f"{provider}/{name}",
                    events=events,
                    run_set_a=run_a,
                    run_set_b=run_b,
                    set_b_to_message=to_msg,
                )
                rows.append((provider, name, list(report.strong_failures), list(report.weak_gaps)))
            except Exception as e:  # noqa: BLE001
                rows.append((provider, name, [f"<harness error: {type(e).__name__}: {e}>"], []))
    return rows


def _run_vendor_truth_axis():
    """Return list of (provider, scenario, vendor_truth_failures, is_known_divergence)."""
    from tests.aggregator_parity.parity_helpers import (
        NON_STREAM_LOADERS,
        compare_structural,
    )
    from tests.aggregator_parity.reconstructor import reconstruct_message_from_agui
    from tests.aggregator_parity.sse_loader import load_recording
    from tests.aggregator_parity.test_stream_vs_non_stream import (
        KNOWN_VENDOR_TRUTH_DIVERGENCES,
        _set_a_runner,
    )

    rows = []
    for provider in sorted(NON_STREAM_LOADERS.keys()):
        rec_dir = FIXTURES_ROOT / provider / "recordings"
        if not rec_dir.is_dir():
            continue
        for non_stream_path in sorted(rec_dir.glob("*.non_stream.json")):
            scenario = non_stream_path.name.removesuffix(".non_stream.json")
            sse_path = rec_dir / f"{scenario}.sse"
            if not sse_path.is_file():
                continue
            try:
                sse_events = load_recording(provider, scenario)
                run_a = _set_a_runner(provider)
                msg_a = reconstruct_message_from_agui(run_a(sse_events))
                payload = json.loads(non_stream_path.read_text(encoding="utf-8"))
                msg_vendor = NON_STREAM_LOADERS[provider](payload)
                failures = compare_structural(msg_a, msg_vendor)
            except Exception as e:  # noqa: BLE001
                failures = [f"<harness error: {type(e).__name__}: {e}>"]
            is_known = (provider, scenario) in KNOWN_VENDOR_TRUTH_DIVERGENCES
            rows.append((provider, scenario, failures, is_known))
    return rows


def _render(strong_rows, vendor_rows, known_divergent_strong) -> str:
    out: list[str] = []
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    out.append("# Aggregator Parity Gap Report")
    out.append("")
    out.append(f"_Generated: {now} by `tests/aggregator_parity/scripts/gen_gap_report.py`._")
    out.append("")
    out.append("This report is the input list for RFC-0023 §阶段 ②. It enumerates")
    out.append("every field Set A's event stream doesn't carry today (vs. Set B's")
    out.append("`finalize()` output) and every structural divergence between Set A's")
    out.append("aggregation and the vendor's own non-stream JSON response.")
    out.append("")

    # ---- Axis 1: Strong equivalence summary ----
    by_provider_total: Counter[str] = Counter()
    by_provider_pass: Counter[str] = Counter()
    by_provider_xfail: Counter[str] = Counter()
    real_failures: list[tuple[str, str, list[str]]] = []
    known_failures: list[tuple[str, str, list[str]]] = []
    for provider, name, strong_failures, _ in strong_rows:
        by_provider_total[provider] += 1
        if not strong_failures:
            by_provider_pass[provider] += 1
        elif name in known_divergent_strong.get(provider, set()):
            by_provider_xfail[provider] += 1
            known_failures.append((provider, name, strong_failures))
        else:
            real_failures.append((provider, name, strong_failures))

    out.append("## Axis 1 — Set A vs Set B strong equivalence")
    out.append("")
    out.append("| Provider | Pass | Known xfail | Total |")
    out.append("| --- | --- | --- | --- |")
    for p in sorted(by_provider_total):
        out.append(f"| `{p}` | {by_provider_pass[p]} | {by_provider_xfail[p]} | {by_provider_total[p]} |")
    if real_failures:
        out.append("")
        out.append("⚠️ **Unregistered failures (real drift — must fix before §阶段 ③ retires Set B):**")
        out.append("")
        out.append("| Provider | Fixture | Failure |")
        out.append("| --- | --- | --- |")
        for p, n, fs in real_failures:
            for f in fs:
                out.append(f"| `{p}` | `{n}` | {f} |")
    elif not known_failures:
        out.append("")
        out.append("✅ All fixtures pass. No drift between Set A and Set B on the same input.")
    if known_failures:
        out.append("")
        out.append("**Registered known divergences (strict xfail in `KNOWN_DIVERGENT_FIXTURES` — design discussions for §阶段 ②):**")
        out.append("")
        out.append("| Provider | Fixture | Failure |")
        out.append("| --- | --- | --- |")
        for p, n, fs in known_failures:
            for f in fs:
                out.append(f"| `{p}` | `{n}` | {f} |")
    out.append("")

    # ---- Axis 2: Weak gaps aggregated ----
    gap_field_counter: Counter[str] = Counter()
    gap_examples: dict[str, tuple[str, str, str]] = {}
    for provider, name, _, weak_gaps in strong_rows:
        for gap in weak_gaps:
            gap_field_counter[gap.field] += 1
            if gap.field not in gap_examples:
                gap_examples[gap.field] = (provider, name, gap.note)

    out.append("## Axis 2 — Set A weak gaps (target list for §阶段 ②)")
    out.append("")
    out.append("These fields are present on Set B's `finalize()` dict but cannot")
    out.append("be reconstructed from the AG-UI event stream into a UMP `Message`.")
    out.append("Two paths to close them in §阶段 ②: (a) add the fields to `Message`,")
    out.append("or (b) have the gap-checker consume `ModelCallFinishedEvent` from")
    out.append("the agui event stream directly. Either way, the gap is at the")
    out.append("`Message`-shape level even though Set A already emits the metadata.")
    out.append("")
    if not gap_field_counter:
        out.append("✅ Zero weak gaps.")
    else:
        out.append("| Field | Fixtures | Sample provider | Sample fixture | Note |")
        out.append("| --- | --- | --- | --- | --- |")
        for field, count in sorted(gap_field_counter.items(), key=lambda x: (-x[1], x[0])):
            p, n, note = gap_examples[field]
            out.append(f"| `{field}` | {count} | `{p}` | `{n}` | {note} |")
    out.append("")

    # ---- Axis 3: Vendor truth ----
    out.append("## Axis 3 — Set A vs vendor non-stream JSON")
    out.append("")
    if not vendor_rows:
        out.append("_No `<scenario>.non_stream.json` fixture pairs on disk yet._")
        out.append("")
    else:
        green = [r for r in vendor_rows if not r[2]]
        red_unknown = [r for r in vendor_rows if r[2] and not r[3]]
        red_known = [r for r in vendor_rows if r[2] and r[3]]
        out.append(f"- Total pairs: **{len(vendor_rows)}**")
        out.append(f"- Structural match: **{len(green)}**")
        out.append(f"- Known divergences (registered xfail): **{len(red_known)}**")
        out.append(f"- Unregistered failures: **{len(red_unknown)}**")
        out.append("")
        if red_unknown:
            out.append("⚠️ **Unregistered structural divergences** — either fix Set A or add to `KNOWN_VENDOR_TRUTH_DIVERGENCES`:")
            out.append("")
            out.append("| Provider | Scenario | Failure |")
            out.append("| --- | --- | --- |")
            for p, s, fs, _ in red_unknown:
                for f in fs:
                    out.append(f"| `{p}` | `{s}` | {f} |")
            out.append("")
        if red_known:
            out.append("**Known divergences (design discussions for §阶段 ②):**")
            out.append("")
            out.append("| Provider | Scenario | Failure |")
            out.append("| --- | --- | --- |")
            for p, s, fs, _ in red_known:
                for f in fs:
                    out.append(f"| `{p}` | `{s}` | {f} |")
            out.append("")

    # ---- Coverage summary ----
    out.append("## Coverage")
    out.append("")
    out.append("| Provider | Fixtures (axis 1) | Vendor-truth pairs (axis 3) |")
    out.append("| --- | --- | --- |")
    vendor_count_by_provider: Counter[str] = Counter()
    for p, _, _, _ in vendor_rows:
        vendor_count_by_provider[p] += 1
    for p in sorted(by_provider_total):
        out.append(f"| `{p}` | {by_provider_total[p]} | {vendor_count_by_provider[p]} |")
    out.append("")

    out.append("---")
    out.append("")
    out.append("Regenerate: `uv run tests/aggregator_parity/scripts/gen_gap_report.py`")
    return "\n".join(out) + "\n"


def main() -> None:
    print("→ Running axis 1 (Set A vs Set B strong + weak gaps) on all fixtures...")
    strong_rows = _run_strong_axis()
    print(f"  {len(strong_rows)} fixtures")

    print("→ Running axis 3 (Set A vs vendor non-stream) on all fixture pairs...")
    vendor_rows = _run_vendor_truth_axis()
    print(f"  {len(vendor_rows)} pairs")

    known_divergent_strong = _load_known_divergent_per_provider()
    md = _render(strong_rows, vendor_rows, known_divergent_strong)
    OUTPUT_PATH.write_text(md, encoding="utf-8")
    print(f"✓ wrote {OUTPUT_PATH.relative_to(REPO_ROOT)} ({len(md)} bytes)")


if __name__ == "__main__":
    main()
