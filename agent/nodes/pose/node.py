"""``pose`` 节点 —— 姿态 33 点（``feature/frame.raw`` → ``feature/pose``）。

职责
----
- 订阅 ``feature/frame.raw``（本机，只带"去哪读一帧"的指路牌）；
- 从本机共享内存取回 BGR 帧，交给 :class:`PoseBackend` 推理；
- 发布 ``feature/pose``（紧凑、单帧 ≤ 300 字节的 JSON，见 :mod:`.frame_codec`）。

热切换：指路牌里的 ``epoch`` 变化 = 摄像头被换过 → **重建 MediaPipe 时间轴基准**
（任务书 §6.5 硬性要求 2）。

背压：只保留"最新"一份指路牌，处理不过来就丢帧（前端按 ``seq`` 弃旧，绝不排队）。
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agent.core import Node, topics
from agent.nodes.camera.frame_shm import FrameReader

from .backends import DEFAULT_MODEL_NAME, build_backend
from .backends.base import PoseBackend
from .frame_codec import FeatureFrame

__all__ = ["PoseNode", "NODE"]

NODE = None  # 占位，文件末尾赋值为 PoseNode（供 CLI 的 `NODE = ...` 约定）


class PoseNode(Node):
    name = "pose"

    #: 模型文件名（在 ``Config.models_dir`` 下找）
    model_name = DEFAULT_MODEL_NAME
    #: 处理不过来的轮询间隔（秒）
    poll_interval = 0.004
    #: 多久没收到帧就告警 + 重声明订阅（秒）
    _STARVE_WARN_S = 10.0
    #: 连续重声明这么多次还是收不到帧 → 主动退出，交给监督器重启出干净 session
    _STARVE_MAX_REFRESH = 3

    def __init__(
        self,
        config: Any = None,
        *,
        backend: PoseBackend | None = None,
        backend_kind: str = "mediapipe",
    ) -> None:
        super().__init__(config)
        self._backend: PoseBackend | None = backend
        self._backend_kind = backend_kind
        self._reader: FrameReader | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._worker: asyncio.Task[Any] | None = None
        self._latest: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._seq = 0
        self._epoch: int | None = None
        self._error_count = 0
        self._processed = 0
        #: 「饿死」检测：最后一次收到帧的时刻（0 = 从未收到）。用来发现"订阅掉线但节点还活着"
        self._starve_since: float = 0.0
        #: 已重声明订阅的次数（连续饿死计数，够了就请求重启）
        self._refreshes = 0
        #: 已经"放弃并请求重启"（只请求一次，别每轮刷屏）
        self._gave_up = False

    # ── 生命周期 ──────────────────────────────────────────────
    async def setup(self) -> None:
        self._pub_pose = self.publish(topics.FEATURE_POSE)
        self.subscribe(topics.FEATURE_FRAME_RAW, self.on_frame)
        self._reader = FrameReader(logger=self.log)

        if self._backend is None:
            model_path = self.config.models_dir / self.model_name
            self._backend = build_backend(self._backend_kind, model_path=model_path, logger=self.log)
            self._backend.load()  # 失败抛 BackendError → 节点非 0 退出（异常外显）
        self.log.info("姿态后端：%s（关键点 %d）", self._backend.name, self._backend.landmark_count)

        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pose")
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

    # ── 收帧（Zenoh 回调线程）─────────────────────────────────
    def on_frame(self, msg: Any) -> None:
        if not isinstance(msg, dict):
            return
        with self._lock:
            self._latest = msg

    async def _run_worker(self) -> None:
        # 基线：先当作"正在饿死"（收到第一帧就会清零）—— 否则"从来没收到过帧"这种
        # 最要命的故障反而检测不到（_starve_since 会一直是 0）。
        self._starve_since = time.monotonic()
        while True:
            with self._lock:
                desc, self._latest = self._latest, None
            if desc is None:
                # ★ 收不到帧时**必须自己说出来并自愈**（实测踩过）：
                #   单机 zenoh 订阅会"时通时不通"，一旦订阅收不到 frame.raw，
                #   这个循环就会永远空转 —— 节点活着、日志干净、却再也不发 pose，
                #   面板表现就是"骨骼一直空着"，而摄像头其实在正常写帧。
                #   ⚠️ 这里必须用 `self._STARVE_WARN_S`（类属性）—— 写成裸名字会
                #      立刻 NameError，把整个 worker 任务静默弄死（2026-10-05 踩过，
                #      症状就是"骨骼突然又没了、日志完全干净"）。
                await asyncio.sleep(self.poll_interval)
                if self._starve_since and not self._gave_up:
                    starve = time.monotonic() - self._starve_since
                    if starve > self._STARVE_WARN_S:
                        self._refreshes += 1
                        if self._refreshes <= self._STARVE_MAX_REFRESH:
                            self.log.warning(
                                "已经 %.0f 秒没收到任何帧（subscription 可能掉线）→ 重声明订阅（第 %d/%d 次）",
                                starve, self._refreshes, self._STARVE_MAX_REFRESH,
                            )
                            try:
                                self.subscribe(topics.FEATURE_FRAME_RAW, self.on_frame)
                            except Exception as exc:  # noqa: BLE001
                                self.log.warning("重声明订阅失败：%s", exc)
                            self._starve_since = time.monotonic()   # 下一个窗口再判
                        else:
                            # 重声明也救不回来 → 交给监督器重启出一个干净 session。
                            # 只请求**一次**（`_gave_up` 标记），否则每轮都喊一次会刷屏、
                            # 也会跟监督器的重启节奏打架。
                            # 用基类的 `stop()`（幂等、线程安全、走正常退出路径），
                            # 而不是 raise —— 在任务里抛异常会被 asyncio 静默吞掉。
                            self.log.error(
                                "重声明订阅 %d 次仍收不到帧 → 请求节点重启（交给监督器）",
                                self._refreshes,
                            )
                            self._gave_up = True
                            self.stop()
                continue
            self._starve_since = 0.0
            self._refreshes = 0
            self._gave_up = False
            try:
                await self._process(desc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单帧失败不能让节点死
                self._error_count += 1
                if self._error_count <= 3 or self._error_count % 100 == 0:
                    self.log.warning("处理帧失败（第 %d 次）：%s", self._error_count, exc)

    async def _process(self, desc: dict[str, Any]) -> None:
        # 收到帧就记时（用于上面"饿死"判断）
        self._starve_since = 0.0

    async def _process(self, desc: dict[str, Any]) -> None:
        assert self._reader is not None
        frame = self._reader.read(desc)
        if frame is None:
            # ⚠️ 绝不能静默！实测踩过：read() 每帧都返回 None（attach 失败 / 撕裂 / 尺寸不符），
            #    节点照样活着、日志干净、一条 pose 都不发 —— 面板就是"骨骼永远空的"。
            #    这里限流告警，并把指路牌 + **直接 attach 的结果**一起打出来，便于定位。
            self._read_fail = getattr(self, "_read_fail", 0) + 1
            if self._read_fail <= 3 or self._read_fail % 200 == 0:
                name = desc.get("shm")
                need = HEADER_SIZE + int(desc.get("nbytes") or 0)
                why = ""
                try:
                    from multiprocessing import shared_memory as _shm

                    blk = _shm.SharedMemory(name=name)
                    why = f"直接 attach 成功，块 {len(blk.buf)} 字节 / 需要 {need}"
                    blk.close()
                except Exception as exc:  # noqa: BLE001
                    why = f"直接 attach 失败：{type(exc).__name__}: {exc}"
                self.log.warning(
                    "读帧失败（第 %d 次）：shm=%r need=%d；%s；desc=%r",
                    self._read_fail, name, need, why,
                    {k: desc.get(k) for k in ("seq", "epoch", "shape", "dtype")},
                )
            return
        self._read_fail = 0
        epoch = desc.get("epoch")
        if epoch is not None and epoch != self._epoch:
            self._epoch = epoch
            if self._backend is not None:
                self._backend.reset_timeline()
            self.log.info("采集源变化（epoch=%s），已重建时间轴基准", epoch)

        assert self._executor is not None and self._backend is not None
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(self._executor, self._infer, frame)
        if isinstance(result, BaseException):
            self._error_count += 1
            if self._error_count <= 3 or self._error_count % 100 == 0:
                self.log.warning("推理失败（第 %d 次）：%s", self._error_count, result)
            return
        landmarks, visibility = result

        seq = desc.get("seq")
        self._seq = int(seq) + 1 if isinstance(seq, int) else self._seq + 1
        ts = desc.get("ts")
        ts = int(ts) if isinstance(ts, int) else int(time.time() * 1000)
        frame_out = FeatureFrame(
            ts=ts,
            seq=self._seq,
            present=landmarks is not None,
            landmarks=landmarks or [],
            visibility=visibility,
        )
        self._pub_pose.put(frame_out.to_wire())
        self._processed += 1

    def _infer(self, frame: Any) -> Any:
        try:
            assert self._backend is not None
            return self._backend.infer_detail(frame)
        except Exception as exc:  # noqa: BLE001 - 在 executor 线程里捕获，回传主循环
            return exc


NODE = PoseNode

if __name__ == "__main__":  # pragma: no cover - 便于 `python -m agent.nodes.pose`
    raise SystemExit(PoseNode.main())
