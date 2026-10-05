"""``face`` 节点（T2）—— 对外导出 :class:`FaceNode`。

头姿 yaw/pitch/roll（需求 1b）→ 发 ``feature/face``；表情（1c）归 T6。
取帧方式与 pose 一致（``feature/frame.raw`` 指路牌 → 本机共享内存）。

单跑调试：``python -m agent.core.cli run face`` 或 ``python -m agent.nodes.face``。
"""

from __future__ import annotations

from .node import NODE, FaceNode

__all__ = ["FaceNode", "NODE"]
