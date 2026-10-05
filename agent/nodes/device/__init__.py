"""``device`` 节点（T2）—— 对外导出 :class:`DeviceNode`。

设备枚举（M0：去重 / 稳定 ID / 回环标注）→ 发 ``event/device``（快照 + 变化）→
订阅 ``command/select_device``（记选择 / 重发快照）。

单跑调试：``python -m agent.core.cli run device`` 或 ``python -m agent.nodes.device``。
"""

from __future__ import annotations

from .node import NODE, DeviceNode

__all__ = ["DeviceNode", "NODE"]
