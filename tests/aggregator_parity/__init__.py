# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Parity tests for provider stream aggregators.

RFC-0023 §阶段 ①.

Validates that the two parallel aggregator implementations produce equivalent
``Message`` payloads when fed the same provider stream:

- Set A: ``nexau.archs.llm.llm_aggregators.*`` (push-style, emits AG-UI events)
- Set B: ``nexau.archs.main_sub.execution.llm_caller.*StreamAggregator``
  (pull-style, emits ``ModelResponse`` dict via ``finalize()``)

The harness compares both outputs after reducing each to a canonical ``Message``:

- Set A path:  events stream → ``reconstructor.reconstruct_message`` → Message
- Set B path:  ``finalize()`` dict → ``Message.from_*`` → Message

Strong equivalence (must pass): role, content blocks (count, order, type, fields).
Weak equivalence (recorded as gaps): usage, stop_reason, model_name, reasoning
signature, redacted_data — the fields that motivate ``ModelCallFinishedEvent`` and
``ThinkingTextMessage*`` extensions in RFC-0023 §阶段 ②.
"""
