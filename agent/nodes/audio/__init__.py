"""``audio`` 节点（T2）—— 对外导出 :class:`AudioNode`。

麦克风音量 RMS + 分级事件 → 发 ``feature/audio``（``{ts, seq, level, event, text}``）；
订阅 ``command/select_device`` 热切换麦克风。关键词 / 转写归 T6。

单跑调试：``python -m agent.core.cli run audio`` 或 ``python -m agent.nodes.audio``。
"""

from __future__ import annotations

from .node import NODE, AudioNode

__all__ = ["AudioNode", "NODE"]
