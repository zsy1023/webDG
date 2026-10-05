"""面部后端工厂 + 延迟导入（同 pose 的做法，避免顺带拖起 mediapipe）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import FaceBackend, FaceBackendError

__all__ = ["FaceBackend", "FaceBackendError", "build_face_backend", "DEFAULT_MODEL_NAME", "FACE"]

#: 默认模型（478 点 + 52 blendshapes + 4×4 头姿矩阵，3.6MB）
DEFAULT_MODEL_NAME = "face_landmarker.task"

#: 可选后端名
FACE = "mediapipe"


def build_face_backend(
    kind: str | None = FACE,
    *,
    model_path: str | Path,
    logger: Any = None,
    **kwargs: Any,
) -> FaceBackend:
    if kind in (None, FACE, "mp"):
        from .mediapipe_face import MediaPipeFaceBackend

        return MediaPipeFaceBackend(model_path, logger=logger, **kwargs)
    raise FaceBackendError(f"未知面部后端 {kind!r}（当前可选：{FACE}）")
