"""``face`` 节点 —— 面部头姿（``feature/frame.raw`` → ``feature/face``）。

T2 范围：**头姿 yaw / pitch / roll**（需求 1b）。
T6 补上：**表情**（需求 1c）—— 用 MediaPipe 的 52 个 blendshapes 按规则映射成
``emotion`` / ``confidence``（见 :mod:`.emotion`），并做**时间平滑**（取窗口众数，免得网页乱闪）。
blendshapes 本身默认**不外发**（``publish_blendshapes=True`` 时才带，供调试）。

同 pose 的取帧方式：``feature/frame.raw`` 只带指路牌 → 本机共享内存取帧 → 推理；
``epoch`` 变化时重建时间轴基准（并清空表情平滑窗口）。
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agent.core import Node, topics
from agent.nodes.camera.frame_shm import FrameReader

from .backends import DEFAULT_MODEL_NAME, build_face_backend
from .backends.base import FaceBackend
from .emotion import Smoother

__all__ = ["FaceNode", "NODE"]


class FaceNode(Node):
    name = "face"

    model_name = DEFAULT_MODEL_NAME
    poll_interval = 0.006
    #: 表情平滑窗口（帧）。1 = 不平滑
    emotion_window = 7

    def __init__(
        self,
        config: Any = None,
        *,
        backend: FaceBackend | None = None,
        backend_kind: str = "mediapipe",
        publish_blendshapes: bool = False,
        emotion_window: int | None = None,
    ) -> None:
        super().__init__(config)
        self._backend: FaceBackend | None = backend
        self._backend_kind = backend_kind
        self._publish_blendshapes = bool(publish_blendshapes)
        self._smoother = Smoother(
            emotion_window if emotion_window is not None else self.emotion_window
        )
        self._reader: FrameReader | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._worker: asyncio.Task[Any] | None = None
        self._latest: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._seq = 0
        self._epoch: int | None = None
        self._error_count = 0

    async def setup(self) -> None:
        self._pub_face = self.publish(topics.FEATURE_FACE)
        self.subscribe(topics.FEATURE_FRAME_RAW, self.on_frame)
        self._reader = FrameReader(logger=self.log)

        if self._backend is None:
            model_path = self.config.models_dir / self.model_name
            self._backend = build_face_backend(self._backend_kind, model_path=model_path, logger=self.log)
            self._backend.load()
        self.log.info("面部后端：%s", self._backend.name)

        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="face")
        self._worker = asyncio.create_task(self._run_worker())

    async def teardown(self) -> None:
        task, self._worker = self._worker, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False)
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        if self._backend is not None:
            self._backend.close()

    def on_frame(self, msg: Any) -> None:
        if not isinstance(msg, dict):
            return
        with self._lock:
            self._latest = msg

    async def _run_worker(self) -> None:
        while True:
            with self._lock:
                desc, self._latest = self._latest, None
            if desc is None:
                await asyncio.sleep(self.poll_interval)
                continue
            try:
                await self._process(desc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._error_count += 1
                if self._error_count <= 3 or self._error_count % 100 == 0:
                    self.log.warning("处理帧失败（第 %d 次）：%s", self._error_count, exc)

    async def _process(self, desc: dict[str, Any]) -> None:
        assert self._reader is not None
        frame = self._reader.read(desc)
        if frame is None:
            return
        epoch = desc.get("epoch")
        if epoch is not None and epoch != self._epoch:
            self._epoch = epoch
            if self._backend is not None:
                self._backend.reset_timeline()
            self._smoother.reset()  # 换采集源 → 别把上一个人的表情带过来

        assert self._executor is not None and self._backend is not None
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(self._executor, self._infer, frame)
        if isinstance(result, BaseException):
            self._error_count += 1
            if self._error_count <= 3 or self._error_count % 100 == 0:
                self.log.warning("面部推理失败（第 %d 次）：%s", self._error_count, result)
            return

        seq = desc.get("seq")
        self._seq = int(seq) + 1 if isinstance(seq, int) else self._seq + 1
        ts = desc.get("ts")
        ts = int(ts) if isinstance(ts, int) else int(time.time() * 1000)
        present = bool(result)
        blendshapes = (result or {}).get("blendshapes") if present else None
        # 需求 1c（T6）：52 个 blendshape → 情绪；拿不到就 neutral
        emotion, confidence = self._smoother.feed(blendshapes)
        self._pub_face.put(
            {
                "ts": ts,
                "seq": self._seq,
                "present": present,
                "head": (result or {}).get("head") if present else None,
                "emotion": emotion if present else None,
                "confidence": round(confidence, 3) if present else None,
                "blendshapes": blendshapes if self._publish_blendshapes else None,
            }
        )

    def _infer(self, frame: Any) -> Any:
        try:
            assert self._backend is not None
            return self._backend.infer(frame)
        except Exception as exc:  # noqa: BLE001
            return exc


NODE = FaceNode

if __name__ == "__main__":  # pragma: no cover - 便于 `python -m agent.nodes.face`
    raise SystemExit(FaceNode.main())
