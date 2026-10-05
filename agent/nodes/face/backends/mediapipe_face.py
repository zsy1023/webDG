"""MediaPipe Tasks ``FaceLandmarker`` 后端 —— :class:`FaceBackend` 的默认实现。

模型：项目根 ``models/face_landmarker.task``（478 点 + 52 blendshapes + 4×4 头姿矩阵）。

T2 用它算**头姿 yaw/pitch/roll**（需求 1b）；blendshapes 一并取回但默认不随消息外发
（表情 1c 归 T6）。同样遵守 MediaPipe VIDEO 模式"时间戳严格递增"的要求。
"""

from __future__ import annotations

import math
import os
import time
from typing import Any

from .base import FaceBackend, FaceBackendError

__all__ = ["MediaPipeFaceBackend"]


class MediaPipeFaceBackend(FaceBackend):
    name = "mediapipe-face"

    def __init__(
        self,
        model_path: str | os.PathLike[str],
        *,
        num_faces: int = 1,
        min_face_detection_confidence: float = 0.5,
        min_tracking_confidence: float = 0.5,
        logger: Any = None,
    ) -> None:
        self.model_path = str(model_path)
        self.num_faces = int(num_faces)
        self.min_face_detection_confidence = float(min_face_detection_confidence)
        self.min_tracking_confidence = float(min_tracking_confidence)
        self.log = logger
        self._mp: Any = None
        self._landmarker: Any = None
        self._ts_base: int | None = None
        self._last_ts = 0

    def load(self) -> None:
        try:
            import mediapipe as mp
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision
        except ImportError as exc:  # pragma: no cover - 环境问题
            raise FaceBackendError(f"未安装 mediapipe：{exc}") from exc
        if not os.path.exists(self.model_path):
            raise FaceBackendError(
                f"面部模型不存在：{self.model_path}\n"
                f"  用 `python tools/download_models.py` 下载（走代理 http://127.0.0.1:7897）"
            )
        # ⚠️ MediaPipe 的 C++ 层**打不开含非 ASCII 字符的路径**（实测：项目在 `web控制\` 下时
        #    `model_asset_path` 报 FileNotFoundError；同一文件放纯 ASCII 路径就 OK）。
        #    统一读成 bytes 走 `model_asset_buffer`，绕过路径编码问题。
        try:
            with open(self.model_path, "rb") as fh:
                model_bytes = fh.read()
        except OSError as exc:
            raise FaceBackendError(f"读取面部模型失败：{self.model_path}（{exc}）") from exc
        base_options = mp_python.BaseOptions(model_asset_buffer=model_bytes)
        options = vision.FaceLandmarkerOptions(
            base_options=base_options,
            running_mode=vision.RunningMode.VIDEO,
            num_faces=self.num_faces,
            min_face_detection_confidence=self.min_face_detection_confidence,
            min_face_presence_confidence=self.min_face_detection_confidence,
            min_tracking_confidence=self.min_tracking_confidence,
            output_face_blendshapes=True,
            output_facial_transformation_matrixes=True,
        )
        try:
            self._landmarker = vision.FaceLandmarker.create_from_options(options)
        except Exception as exc:  # noqa: BLE001
            raise FaceBackendError(f"创建 FaceLandmarker 失败：{exc}") from exc
        self._mp = mp
        self.reset_timeline()

    def reset_timeline(self, base_ms: int | None = None) -> None:
        self._ts_base = int(base_ms) if base_ms is not None else None
        self._last_ts = 0

    def _next_timestamp(self) -> int:
        now = int(time.monotonic() * 1000)
        if self._ts_base is None:
            self._ts_base = now
        ts = now
        if ts <= self._last_ts:
            ts = self._last_ts + 1
        self._last_ts = ts
        return ts

    def infer(self, frame_bgr: Any) -> dict[str, Any] | None:
        if self._landmarker is None:
            raise FaceBackendError("FaceBackend 尚未 load()")
        rgb = self._to_rgb(frame_bgr)
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        result = self._landmarker.detect_for_video(image, self._next_timestamp())

        matrices = getattr(result, "facial_transformation_matrixes", None)
        if not matrices:
            return None
        head = _head_angles(matrices[0])
        blendshapes = _blendshapes(getattr(result, "face_blendshapes", None))
        return {"head": head, "blendshapes": blendshapes}

    @staticmethod
    def _to_rgb(frame_bgr: Any) -> Any:
        import numpy as np

        arr = np.asarray(frame_bgr)
        if arr.ndim == 3 and arr.shape[2] == 3:
            return np.ascontiguousarray(arr[:, :, ::-1])
        if arr.ndim == 3 and arr.shape[2] == 4:
            return np.ascontiguousarray(arr[:, :, 2::-1])
        return np.ascontiguousarray(arr)

    def close(self) -> None:
        landmarker, self._landmarker = self._landmarker, None
        if landmarker is not None:
            try:
                landmarker.close()
            except Exception as exc:  # noqa: BLE001
                if self.log is not None:
                    self.log.debug("关闭 FaceLandmarker 失败：%s", exc)


def _head_angles(matrix: Any) -> dict[str, float]:
    """4×4 变换矩阵 → 头姿角度（度）。MediaPipe 约定，ZYX 欧拉分解。

    ⚠️ 矩阵行列/朝向约定因版本而异，这里是通用分解（预览够用）；若要精确对齐，
    T6 可在此校准（不影响协议 —— 出去的永远是 yaw/pitch/roll 三个角度）。
    """
    import numpy as np

    m = np.asarray(matrix, dtype="float64").reshape(4, 4)
    r = m[:3, :3]
    sy = math.hypot(float(r[0, 0]), float(r[1, 0]))
    if sy > 1e-6:
        pitch = math.degrees(math.atan2(-float(r[2, 0]), sy))
        yaw = math.degrees(math.atan2(float(r[1, 0]), float(r[0, 0])))
        roll = math.degrees(math.atan2(float(r[2, 1]), float(r[2, 2])))
    else:  # 万向锁
        pitch = math.degrees(math.atan2(-float(r[2, 0]), sy))
        yaw = 0.0
        roll = math.degrees(math.atan2(-float(r[1, 2]), float(r[1, 1])))
    return {
        "yaw": round(yaw, 1),
        "pitch": round(pitch, 1),
        "roll": round(roll, 1),
    }


def _blendshapes(raw: Any) -> dict[str, float] | None:
    """``face_blendshapes`` → ``{类别名: 分数}``；为空返回 ``None``。"""
    if not raw:
        return None
    categories = raw[0] if isinstance(raw, (list, tuple)) and raw else raw
    out: dict[str, float] = {}
    for item in categories:
        name = getattr(item, "category_name", None)
        score = getattr(item, "score", None)
        if name is not None and score is not None:
            out[str(name)] = round(float(score), 3)
    return out or None
