# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""JSON file tracer for local trace persistence."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from nexau.archs.tracer.core import BaseTracer, Span, SpanType

logger = logging.getLogger(__name__)

# 默认输出目录
_DEFAULT_OUTPUT_DIR = Path("traces")


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 格式字符串，用于文件名。"""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _serialize_for_json(data: Any) -> Any:  # noqa: ANN401
    """将复杂对象序列化为 JSON 兼容格式。

    - 基本类型 (str, int, float, bool, None) 原样返回。
    - dict/list/tuple 递归处理。
    - 其他类型通过 str() 转换。

    Args:
        data: 要序列化的数据

    Returns:
        JSON 兼容的表示
    """
    if data is None:
        return None
    if isinstance(data, str):
        return data
    if isinstance(data, bool):
        return data
    if isinstance(data, (int, float)):
        return data
    if isinstance(data, dict):
        mapping = cast(Mapping[str, Any], data)
        result: dict[str, Any] = {}
        for k, v in mapping.items():
            result[str(k)] = _serialize_for_json(v)
        return result
    if isinstance(data, (list, tuple)):
        sequence = cast(Sequence[Any], data)
        result_list: list[Any] = []
        for item in sequence:
            result_list.append(_serialize_for_json(item))
        return result_list
    # 对于不可 JSON 序列化的对象，转为字符串
    try:
        return json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(data)


class JsonFileTracer(BaseTracer):
    """将 trace 数据持久化到本地 JSON 文件的 tracer 适配器。

    所有 span 在内存中累积，调用 :meth:`flush` 或 :meth:`shutdown` 时写入磁盘。
    输出为嵌套的 JSON 结构，保留 span 之间的父子关系。

    使用示例::

        tracer = JsonFileTracer(output_dir="my_traces")
        with TraceContext(tracer, "my_agent", SpanType.AGENT) as span:
            response = agent.run("Hello")
        tracer.flush()  # 写入 my_traces/trace_<session>_<timestamp>.json

    文件命名规则::

        trace_<session_id>_<timestamp>.json

    Attributes:
        output_dir: JSON 文件输出目录
        session_id: 当前会话 ID（可通过 set_session_id 更新）
        pretty_print: 是否格式化 JSON 输出（默认 True，便于人工阅读）
        enabled: 是否启用 tracing
    """

    def __init__(
        self,
        output_dir: str | Path | None = None,
        session_id: str | None = None,
        pretty_print: bool = True,
        enabled: bool = True,
    ):
        """初始化 JSON 文件 tracer。

        Args:
            output_dir: 输出目录路径，默认为 ``traces/``
            session_id: 会话标识符，用于文件命名
            pretty_print: 是否格式化 JSON 输出
            enabled: 是否启用 tracing（False 时所有操作为 no-op）
        """
        self.enabled = enabled
        self.output_dir = Path(output_dir or _DEFAULT_OUTPUT_DIR)
        self.session_id = session_id or str(uuid.uuid4())
        self.pretty_print = pretty_print

        # 内存中的 span 存储
        self._spans: dict[str, Span] = {}
        # 父子关系映射: parent_id -> [child_id, ...]
        self._children: dict[str, list[str]] = {}
        # 根 span id 列表（parent_id 为 None 的 span）
        self._root_span_ids: list[str] = []

        if self.enabled:
            logger.info(
                "JsonFileTracer initialized (output_dir=%s, session_id=%s)",
                self.output_dir,
                self.session_id,
            )

    # ------------------------------------------------------------------
    # BaseTracer 接口实现
    # ------------------------------------------------------------------

    def start_span(
        self,
        name: str,
        span_type: SpanType,
        inputs: dict[str, Any] | None = None,
        parent_span: Span | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Span:
        """创建并记录一个 span。

        Args:
            name: span 名称
            span_type: span 类型
            inputs: 输入数据
            parent_span: 父 span
            attributes: 元数据/属性

        Returns:
            新创建的 Span 对象
        """
        span_id = str(uuid.uuid4())
        now = datetime.now().timestamp()

        # 确定父 span id
        parent_id = None
        if parent_span is not None:
            # 优先使用 vendor_obj 中存储的 id（跨 CompositeTracer 场景），
            # 否则使用 parent_span.id
            if isinstance(parent_span.vendor_obj, str):
                parent_id = parent_span.vendor_obj
            else:
                parent_id = parent_span.id

        span = Span(
            id=span_id,
            name=name,
            type=span_type,
            parent_id=parent_id,
            start_time=now,
            inputs=inputs or {},
            attributes=attributes or {},
            vendor_obj=span_id,  # 存储自己的 id，方便 end_span 查找
        )

        if self.enabled:
            self._spans[span_id] = span
            if parent_id is None:
                self._root_span_ids.append(span_id)
            else:
                self._children.setdefault(parent_id, []).append(span_id)

        return span

    def end_span(
        self,
        span: Span,
        outputs: Any = None,
        error: Exception | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """结束一个 span，记录结束时间和输出。

        Args:
            span: 要结束的 span
            outputs: 输出数据
            error: 异常信息（如有）
            attributes: 附加属性
        """
        if not self.enabled:
            return

        # 从内存中查找已存储的 span
        lookup_id = str(span.vendor_obj) if span.vendor_obj else span.id
        stored_span = self._spans.get(lookup_id)
        if stored_span is None:
            # fallback: 用 span.id 查找
            stored_span = self._spans.get(span.id)
        if stored_span is None:
            logger.debug("Span not found in store: %s", span.id)
            return

        stored_span.end_time = datetime.now().timestamp()

        if outputs is not None:
            stored_span.outputs = (
                outputs if isinstance(outputs, dict) else {"result": outputs}
            )

        if error is not None:
            stored_span.error = str(error)

        if attributes:
            stored_span.attributes = {**stored_span.attributes, **attributes}

    def flush(self) -> None:
        """将所有累积的 trace 数据写入 JSON 文件。

        写入后清空内存中的 span 数据，避免重复写入。
        """
        if not self.enabled:
            return

        if not self._root_span_ids:
            logger.debug("No spans to flush")
            return

        # 1. 确保输出目录存在
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 2. 构建文件名
        filename = f"trace_{self.session_id}_{_now_iso()}.json"
        filepath = self.output_dir / filename

        # 3. 构建嵌套的 trace 结构
        traces = self._build_trace_tree()

        # 4. 写入文件
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(
                    traces,
                    f,
                    ensure_ascii=False,
                    indent=2 if self.pretty_print else None,
                    default=str,
                )
            logger.info("Trace data written to %s (%d root spans)", filepath, len(traces))
        except OSError as e:
            logger.warning("Failed to write trace file %s: %s", filepath, e)
            return

        # 5. 清空内存（已持久化）
        self._spans.clear()
        self._children.clear()
        self._root_span_ids.clear()

    def shutdown(self) -> None:
        """关闭 tracer，先 flush 再清理资源。"""
        if self.enabled:
            try:
                self.flush()
                logger.info("JsonFileTracer shutdown complete")
            except Exception as e:
                logger.warning("JsonFileTracer shutdown error: %s", e)

    def set_session_id(self, session_id: str) -> None:
        """设置当前会话 ID。

        由 Agent 调用以同步规范的 session_id，
        确保 JSON 文件名与框架其他组件一致。

        Args:
            session_id: 规范的会话标识符
        """
        self.session_id = session_id
        logger.debug("JsonFileTracer session_id updated to %s", session_id)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _build_trace_tree(self) -> list[dict[str, Any]]:
        """将内存中的 span 构建为嵌套的 tree 结构。

        从根 span 开始递归构建，每个节点包含其所有子 span。

        Returns:
            嵌套的 trace 树列表
        """

        def _span_to_dict(span: Span) -> dict[str, Any]:
            """将单个 span 转换为字典，并递归包含其子 span。"""
            child_ids = self._children.get(span.id, [])
            return {
                "id": span.id,
                "name": span.name,
                "type": span.type.value,
                "parent_id": span.parent_id,
                "start_time": span.start_time,
                "end_time": span.end_time,
                "duration_ms": span.duration_ms(),
                "inputs": _serialize_for_json(span.inputs),
                "outputs": _serialize_for_json(span.outputs),
                "attributes": _serialize_for_json(span.attributes),
                "error": span.error,
                "children": [
                    _span_to_dict(self._spans[child_id])
                    for child_id in child_ids
                    if child_id in self._spans
                ],
            }

        return [
            _span_to_dict(self._spans[root_id])
            for root_id in self._root_span_ids
            if root_id in self._spans
        ]

    def get_trace_tree(self) -> list[dict[str, Any]]:
        """获取当前内存中的 trace 树结构（不写入文件）。

        Returns:
            嵌套的 trace 树列表
        """
        return self._build_trace_tree()

    @property
    def span_count(self) -> int:
        """当前内存中的 span 数量。"""
        return len(self._spans)

    @property
    def root_count(self) -> int:
        """当前内存中的根 span 数量。"""
        return len(self._root_span_ids)
