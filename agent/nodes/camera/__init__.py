"""``camera`` 节点（T2）—— 对外导出 :class:`CameraNode`。

- 采集摄像头 → 本机共享内存（原始像素**不出本机**）
- 发布 ``feature/frame.raw``（只带指路牌，**永不跨机**）
- 订阅 ``command/select_device``（热切换）

单跑调试：``python -m agent.core.cli run camera`` 或 ``python -m agent.nodes.camera``。

``frame_shm`` 是本机帧总线（产者 :class:`FrameBuffer` / 消费者 :class:`FrameReader`），
pose / face 节点从这里导入 :class:`FrameReader`。
"""

from __future__ import annotations

from .frame_shm import FrameBuffer, FrameReader, FrameShmError
from .node import NODE, CameraNode

__all__ = ["CameraNode", "NODE", "FrameBuffer", "FrameReader", "FrameShmError"]
