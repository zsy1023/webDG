"""姿态后端工厂 + 延迟导入。

把 ``mediapipe`` 的 import 推迟到真正要建后端时 —— 这样 ``agent.nodes.pose`` 的
导入（CLI 发现节点、单测收集、前端桥接）都不会顺带把 mediapipe / TFLite 拖起来。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import BackendError, PoseBackend

__all__ = ["BackendError", "PoseBackend", "build_backend", "DEFAULT_MODEL_NAME", "MEDIA"]

#: 默认模型（lite：5.5MB，CPU 可跑，实测 ~12ms@640x480）
DEFAULT_MODEL_NAME = "pose_landmarker_lite.task"

#: 可选后端名
MEDIA = "mediapipe"


def build_backend(
    kind: str | None = MEDIA,
    *,
    model_path: str | Path,
    logger: Any = None,
    **kwargs: Any,
) -> PoseBackend:
    """按名字建后端（**不**自动 ``load()``，由调用方决定何时加载）。

    ``kind="mediapipe"`` → :class:`MediaPipePoseBackend`。
    将来新增 RTMPose / YOLO 时在这里加一个分支即可。
    """
    if kind in (None, MEDIA, "mp"):
        from .mediapipe_backend import MediaPipePoseBackend

        return MediaPipePoseBackend(model_path, logger=logger, **kwargs)
    raise BackendError(f"未知姿态后端 {kind!r}（当前可选：{MEDIA}）")
