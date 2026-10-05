"""``pose`` 节点（T2）—— 对外导出 :class:`PoseNode`。

- 订阅 ``feature/frame.raw``（本机指路牌）→ 本机共享内存取帧 → MediaPipe 推理
- 发布 ``feature/pose``（紧凑 JSON，单帧 ≤ 300 字节）

单跑调试：``python -m agent.core.cli run pose`` 或 ``python -m agent.nodes.pose``。
"""

from __future__ import annotations

from .node import NODE, PoseNode

__all__ = ["PoseNode", "NODE"]
