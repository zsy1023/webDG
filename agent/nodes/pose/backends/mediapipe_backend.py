"""MediaPipe Tasks ``PoseLandmarker`` 后端 —— :class:`PoseBackend` 的默认实现。

模型：项目根 ``models/pose_landmarker_lite.task``（默认）或 ``..._full.task``。
路径由 ``agent.core.config.Config.models_dir`` 解析，**不在模块里另建 models/ 子目录**
（任务书 §3）。

三个实测事实（直接照做，别猜）：

- MediaPipe Tasks 的 VIDEO 模式要求 ``detect_for_video(image, timestamp_ms)`` 的
  时间戳**严格递增** → 本后端自己发号，且保证递增；
- 换摄像头（epoch 变化）时要 `reset_timeline()` 重建基准，否则新基准可能回退 →
  MediaPipe 报 ``timestamp must be monotonically increasing``；
- 输入必须是 **RGB**（``mp.ImageFormat.SRGB``），OpenCV 拿到的是 BGR → 要翻转通道。
"""

from __future__ import annotations

import os
import time
from typing import Any

from .base import BackendError, PoseBackend

__all__ = ["MediaPipePoseBackend"]


class MediaPipePoseBackend(PoseBackend):
    name = "mediapipe-pose"

    def __init__(
        self,
        model_path: str | os.PathLike[str],
        *,
        num_poses: int = 1,
        min_detection_confidence: float = 0.5,
        min_tracking_confidence: float = 0.5,
        logger: Any = None,
    ) -> None:
        self.model_path = str(model_path)
        self.num_poses = int(num_poses)
        self.min_detection_confidence = float(min_detection_confidence)
        self.min_tracking_confidence = float(min_tracking_confidence)
        self.log = logger
        self._mp: Any = None
        self._vision: Any = None
        self._landmarker: Any = None
        self._ts_base: int | None = None
        self._last_ts = 0

    # ── 生命周期 ──────────────────────────────────────────────
    def load(self) -> None:
        try:
            import mediapipe as mp
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision
        except ImportError as exc:  # pragma: no cover - 环境问题
            raise BackendError(
                f"未安装 mediapipe（感知链路的关键依赖）：{exc}\n"
                f"  安装：pip install --proxy http://127.0.0.1:7897 mediapipe"
            ) from exc

        if not os.path.exists(self.model_path):
            raise BackendError(
                f"姿态模型不存在：{self.model_path}\n"
                f"  用 `python tools/download_models.py` 下载（走代理 http://127.0.0.1:7897）"
            )

        # ⚠️ MediaPipe 的 C++ 层**打不开含非 ASCII 字符的路径**。
        #    实测：项目在 `...\Desktop\web控制\` 下时 `model_asset_path` 必报
        #    `FileNotFoundError: Unable to open file at ...`，而**同一个文件**放到纯 ASCII
        #    路径下就正常（model_asset_buffer=bytes 也正常）。
        #    所以这里一律读成 bytes 走 `model_asset_buffer`，绕过路径编码问题。
        try:
            with open(self.model_path, "rb") as fh:
                model_bytes = fh.read()
        except OSError as exc:
            raise BackendError(f"读取姿态模型失败：{self.model_path}（{exc}）") from exc
        base_options = mp_python.BaseOptions(model_asset_buffer=model_bytes)
        options = vision.PoseLandmarkerOptions(
            base_options=base_options,
            running_mode=vision.RunningMode.VIDEO,
            num_poses=self.num_poses,
            min_pose_detection_confidence=self.min_detection_confidence,
            min_pose_presence_confidence=self.min_detection_confidence,
            min_tracking_confidence=self.min_tracking_confidence,
        )
        try:
            self._landmarker = vision.PoseLandmarker.create_from_options(options)
        except Exception as exc:  # noqa: BLE001 - 模型坏 / 版本不兼容都要给出可读错误
            raise BackendError(f"创建 PoseLandmarker 失败：{exc}") from exc
        self._mp = mp
        self._vision = vision
        self.reset_timeline()
        if self.log is not None:
            self.log.info(
                "MediaPipe 姿态后端已加载：%s（%d KB，经 buffer 绕过中文路径）",
                os.path.basename(self.model_path),
                len(model_bytes) // 1024,
            )

    # ── 时间轴 ────────────────────────────────────────────────
    def reset_timeline(self, base_ms: int | None = None) -> None:
        """重建时间戳基准（换摄像头 / 切换采集源后必须调用）。"""
        self._ts_base = int(base_ms) if base_ms is not None else None
        self._last_ts = 0

    def _next_timestamp(self) -> int:
        now = int(time.monotonic() * 1000)
        if self._ts_base is None:
            self._ts_base = now
        ts = now
        if ts <= self._last_ts:  # 保证严格递增
            ts = self._last_ts + 1
        self._last_ts = ts
        return ts

    # ── 推理 ──────────────────────────────────────────────────
    def infer(self, frame_bgr: Any) -> list[list[float]] | None:
        landmarks, _ = self.infer_detail(frame_bgr)
        return landmarks

    def infer_detail(
        self, frame_bgr: Any
    ) -> tuple[list[list[float]] | None, list[float] | None]:
        if self._landmarker is None:
            raise BackendError("PoseBackend 尚未 load()")
        rgb = self._to_rgb(frame_bgr)
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        result = self._landmarker.detect_for_video(image, self._next_timestamp())
        poses = getattr(result, "pose_landmarks", None)
        if not poses:
            return None, None
        landmarks: list[list[float]] = []
        visibility: list[float] = []
        for point in poses[0]:
            landmarks.append([float(point.x), float(point.y), float(point.z)])
            visibility.append(float(getattr(point, "visibility", 0.0) or 0.0))
        return landmarks, visibility

    @staticmethod
    def _to_rgb(frame_bgr: Any) -> Any:
        import numpy as np

        arr = np.asarray(frame_bgr)
        if arr.ndim == 3 and arr.shape[2] == 3:
            return np.ascontiguousarray(arr[:, :, ::-1])  # BGR → RGB
        if arr.ndim == 3 and arr.shape[2] == 4:
            return np.ascontiguousarray(arr[:, :, 2::-1])  # BGRA → RGB
        return np.ascontiguousarray(arr)

    def close(self) -> None:
        landmarker, self._landmarker = self._landmarker, None
        if landmarker is not None:
            try:
                landmarker.close()
            except Exception as exc:  # noqa: BLE001
                if self.log is not None:
                    self.log.debug("关闭 PoseLandmarker 失败：%s", exc)
