"""姿态模型后端的抽象层 —— **硬性要求**：换模型不伤上层。

任务书 §6：``PoseBackend`` 必须是一个抽象接口。默认实现是 MediaPipe；将来要换
RTMPose / YOLO-pose，只需**新增一个实现**，pose 节点、FeatureFrame 编码、前端
一律不动。这就是"换模型不伤上层"的落地。

约定
----
- ``infer()`` 返回 **33×3 归一化**坐标 ``[[x, y, z], ...]``，``x/y ∈ [0, 1]``，
  ``z`` 是相对深度（MediaPipe 约定，通常很小、可正可负）；无人则返回 ``None``。
- **绝不返回像素坐标**（协议里一律归一化，任务书 §5 要求 3）。
- 时间轴：MediaPipe 的 VIDEO 模式要求**严格递增**的时间戳，所以后端自己维护时间轴，
  换摄像头（epoch 变化）时由上层调用 ``reset_timeline()`` 重建基准。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

__all__ = ["BackendError", "PoseBackend"]


class BackendError(RuntimeError):
    """后端加载 / 推理失败（缺依赖、模型缺失、模型内部错误）。"""


class PoseBackend(ABC):
    """姿态模型抽象。默认实现见 :class:`~agent.nodes.pose.backends.mediapipe_backend.MediaPipePoseBackend`。"""

    #: 后端展示名（日志 / 诊断用）
    name: str = "pose-backend"

    @abstractmethod
    def load(self) -> None:
        """加载模型，准备好推理。失败抛 :class:`BackendError`。"""

    @abstractmethod
    def infer(self, frame_bgr: Any) -> list[list[float]] | None:
        """输入 BGR ``ndarray``，返回 33×3 归一化关键点；无人则 ``None``。"""

    def infer_detail(
        self, frame_bgr: Any
    ) -> tuple[list[list[float]] | None, list[float] | None]:
        """``infer()`` 的增强版：额外返回逐点 ``visibility``（可选，默认不提供）。

        默认实现只调 ``infer()`` 并把 visibility 置 ``None``；会输出可见度的后端
        （MediaPipe）覆盖它，让前端能淡显被遮挡的点。
        """
        return self.infer(frame_bgr), None

    def reset_timeline(self, base_ms: int | None = None) -> None:
        """重建时间轴基准（换摄像头 / epoch 变化时调用）。默认空实现。"""
        return None

    def close(self) -> None:
        """释放模型资源。默认空实现。"""
        return None

    @property
    def landmark_count(self) -> int:
        """期望的关键点数量（BlazePose = 33）。"""
        return 33
