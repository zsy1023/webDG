"""面部模型后端抽象 —— 与 :class:`~agent.nodes.pose.backends.base.PoseBackend` 同构。

T2 只做**头姿**（需求 1b：yaw / pitch / roll）；表情（需求 1c，52 blendshapes）归 T6。
把抽象留在这里，T6 换/加实现时上层（face 节点）不用改。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

__all__ = ["FaceBackend", "FaceBackendError"]


class FaceBackendError(RuntimeError):
    """面部后端加载 / 推理失败。"""


class FaceBackend(ABC):
    #: 后端展示名
    name: str = "face-backend"

    @abstractmethod
    def load(self) -> None:
        """加载模型。失败抛 :class:`FaceBackendError`。"""

    @abstractmethod
    def infer(self, frame_bgr: Any) -> dict[str, Any] | None:
        """输入 BGR ``ndarray``，返回一帧面部特征 dict；无人脸则 ``None``。

        约定返回（字段可缺省）：

        .. code-block:: python

            {
              "head": {"yaw": 3.2, "pitch": -1.1, "roll": 0.4},  # 角度
              "blendshapes": {"mouthOpen": 0.7, ...} | None,     # T6 用
            }
        """

    def reset_timeline(self, base_ms: int | None = None) -> None:
        """重建时间戳基准（换摄像头 / epoch 变化时调用）。默认空实现。"""
        return None

    def close(self) -> None:
        """释放模型资源。默认空实现。"""
        return None
