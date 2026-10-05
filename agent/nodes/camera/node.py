"""``camera`` 节点 —— 摄像头采集（发 ``feature/frame.raw``，**永不跨机**）。

职责
----
- 打开选中的摄像头（多设备、热切换，见 :mod:`agent.core.devices`）；
- 按 ``target_fps`` 抓帧，写进**本机共享内存**，发布一句**不含像素**的指路牌到
  ``feature/frame.raw``（见 :mod:`.frame_shm`）；
- 订阅 ``command/select_device``：收到 ``{"kind":"camera","id":...}`` 就**热切换**，
  切换后 ``epoch`` 变化 → 下游（pose / face）据此重建 MediaPipe 时间轴；
- 发 ``event/link``（``kind="camera"``）：**当前在用哪台 + 实测帧率 + 打不开的原因**。
  面板上那盏「摄像头」灯读它 —— 以前这些只在日志里，用户看到的是"选了没反应"。

打不开怎么办（2026-10 实测踩过）
--------------------------------
本机 `HDI Webcam USB` 会被枚举成第一台（"外置"），但它要么只有 1 FPS、要么**设备忙**。
原来的行为是：每秒重试同一台、每秒刷一条 warning、**永远不去试别的** —— 于是整条
"摄像头→姿态"链路静默死掉，而面板上一个字都不说。现在：

- **自动**（没显式选过）：打不开就**换下一台可用设备**并说明"原来那台为什么不行"；
- **手动选的**：打不开就**明确报错并保留原来在用的那台**（不偷偷换走用户的选择）；
- 重试**退避**、日志只在状态变化时打（不再每秒刷屏）。

隐私：像素永远不出本进程的内存；``feature/frame.raw`` 既不在跨机白名单里，
payload 里也没有像素（需求 1d）。
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agent.core import Node, topics
from agent.core.devices import (
    CAMERA,
    CameraSource,
    DeviceError,
    DeviceRegistry,
)

from .frame_shm import FrameBuffer

__all__ = ["CameraNode", "NODE"]

#: 打不开时的重试退避（秒）—— 别每秒刷屏，也别死等
_RETRY_BACKOFF_S: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)
#: `event/link` 重播间隔（面板晚开也能看到状态）
#: `event/link` 重播间隔（秒）
#: ⚠️ 实测：机器满负载（11 个节点 + MediaPipe）时，面板那个进程只能收到约 10% 的消息，
#: 所以**重播要勤**（丢了 1 秒就补一条），面板上的实测帧率不会长时间停在旧值。
_LINK_REPEAT_S = 1.0
#: 用着"不对的"摄像头时，多久回头再试一次（见 :meth:`CameraNode._maybe_recover`）
_RECOVER_EVERY_S = 20.0
#: 实测帧率低于这个值 → 认为这台"打不出帧"（本机内置 `ACER HD User Facing` 实测只有 ~1 FPS）
_MIN_OK_FPS = 4.0
#: 低帧率**持续**这么久才动手（别被启动抖动骗了）
_SLOW_GRACE_S = 8.0


class CameraNode(Node):
    name = "camera"

    def __init__(
        self,
        config: Any = None,
        *,
        device_id: str | None = None,
        source: str | None = None,
        target_fps: int = 15,
        width: int = 640,
        height: int = 480,
        backend: str = "dshow",
        registry: DeviceRegistry | None = None,
    ) -> None:
        super().__init__(config)
        #: 选中的稳定 ID（``cam:<设备名>`` 或 ``cam:file:<绝对路径>``）；None = 自动挑第一台
        self._device_id = device_id or source
        self._target_fps = max(1, int(target_fps))
        self._width = int(width)
        self._height = int(height)
        self._backend = backend
        self._registry = registry
        self._source: CameraSource | None = None
        self._buffer: FrameBuffer | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._worker: asyncio.Task[Any] | None = None
        self._seq = 0
        self._frames = 0
        self._fps = 0.0
        self._last_fps_ts = 0.0
        self._last_fps_window = 0.0
        #: 用户显式选过设备没有（决定"打不开"时能不能替他换一台）
        self._user_chose = bool(self._device_id)
        #: 用户点名要用的那台 —— 当时打不开也记着，后台会一直回头重试它
        self._wanted_id: str = self._device_id or ""
        #: 上次"回头再试"的时刻（见 :meth:`_maybe_recover`）
        self._recover_at = 0.0
        #: 帧率低于 :data:`_MIN_OK_FPS` 的起始时刻（0 = 正常）；持续够久就换设备
        self._slow_since = 0.0
        self._pub_link: Any = None
        self._link_task: asyncio.Task[Any] | None = None
        self._retry_index = 0
        self._last_link: dict[str, Any] | None = None
        #: 最近一次失败原因（**采集正常时也要让面板看得见"上一次为什么没成"**）
        self._last_error = ""

    # ── 生命周期 ──────────────────────────────────────────────
    async def setup(self) -> None:
        if self._registry is None:
            self._registry = DeviceRegistry(logger=self.log)  # 构造即枚举（见 devices.py）
        self._buffer = FrameBuffer(logger=self.log)
        self._source = CameraSource(
            self._registry,
            self._device_id,
            width=self._width,
            height=self._height,
            fps=self._target_fps,
            backend=self._backend,
        )
        self._pub = self.publish(topics.FEATURE_FRAME_RAW)
        self._pub_link = self.publish(topics.EVENT_LINK)     # ★ 面板上的「摄像头」灯
        self.subscribe(topics.COMMAND_SELECT_DEVICE, self.on_select)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="camera")

        cameras = self._registry.list(CAMERA)
        self.log.info(
            "发现 %d 个摄像头：%s",
            len(cameras),
            " | ".join(d.name for d in cameras) or "（无）",
        )
        if self._registry.last_error(CAMERA):
            self.log.warning("摄像头枚举告警：%s", self._registry.last_error(CAMERA))

        self._ensure_open()
        self._worker = asyncio.create_task(self._capture_loop())
        self._link_task = asyncio.create_task(self._link_loop())

    async def teardown(self) -> None:
        for attr in ("_link_task", "_worker"):
            task = getattr(self, attr, None)
            setattr(self, attr, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False)
        if self._source is not None:
            self._source.close()
        if self._buffer is not None:
            self._buffer.close()
            self._buffer = None

    # ── 链路状态（面板上的「摄像头」灯）───────────────────────
    async def _link_loop(self) -> None:
        while True:
            await asyncio.sleep(_LINK_REPEAT_S)
            await self._maybe_recover()
            self.publish_link()

    async def _maybe_recover(self) -> None:
        """回头再试"该在用、但当时打不开或打不出帧"的那台摄像头。

        三种情形都要救（实测用户都卡过）：

        1. **用户点名的**那台当时设备忙 → 原来只是保住旧设备 + 报错，**再也不会切回去**；
        2. 自动模式下只能落到**虚拟摄像头**（OBS Virtual Camera，画面里没人 → 姿态没数据）；
        3. 在用的那台**打不出帧**（本机内置 `ACER HD User Facing` 实测只有 ~1 FPS）——
           链路"能跑"，但骨骼画布基本不动。

        ⚠️ 打开/切换放到**线程池**里做：`cv2.VideoCapture(index, CAP_DSHOW)` 实测能阻塞好几秒，
        直接在事件循环里调会把整个节点（连 zenoh 回调一起）卡住 —— 那正是"启动极慢 /
        点了没反应"的来源之一。
        """
        source, registry = self._source, self._registry
        if source is None or registry is None:
            return
        current = source.current
        current_id = current.id if current is not None else ""
        want = self._wanted_id
        slow = bool(self._slow_since) and (time.monotonic() - self._slow_since) > _SLOW_GRACE_S

        if want and current_id == want and not slow:
            return                          # 已经就是用户要的那台，且帧率正常
        if not want and not slow and not (current is not None and current.detail.get("virtual")):
            return                          # 没点名、没落到虚拟头、帧率也正常 → 不动
        now = time.monotonic()
        if now - self._recover_at < _RECOVER_EVERY_S:
            return
        self._recover_at = now

        if want and current_id != want:
            targets = [want]                # 用户点名的那台还没在手 → 只试它
        elif want and slow:
            # 用户点的那台一直只有 1 FPS：**不擅自换走**，只在他那一行说明白
            self._last_error = (
                f"{current.name if current is not None else want} 实测只有 {self._fps:.1f} FPS"
                "（这台出不了帧）—— 在下拉里换一台再点「连接」"
            )
            self.publish_link()
            return
        else:
            # 只挑**真**摄像头（虚拟摄像头正是要离开的那类）
            targets = [
                d.id
                for d in registry.list(CAMERA)
                if d.available and not d.detail.get("virtual") and d.id != current_id
            ]
        for device_id in targets:
            try:
                await self._switch_in_thread(device_id)
            except DeviceError:
                continue
            self._device_id = device_id
            self._errors.clear()
            self._last_error = ""
            self._retry_index = 0
            self._frames = 0
            self._last_fps_ts = time.monotonic()
            self._slow_since = 0.0
            name = source.current.name if source.current else device_id
            self.log.info("摄像头已切到 %s（后台重试成功）", name)
            self.publish_link()
            return

    async def _switch_in_thread(self, device_id: str) -> None:
        """在线程池里切摄像头（别阻塞事件循环，见 :meth:`_maybe_recover`）。"""
        assert self._source is not None
        executor = self._executor
        if executor is None:
            self._source.switch(device_id)
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(executor, self._source.switch, device_id)

    def publish_link(self) -> None:
        """发一条 ``event/link``（摄像头那盏灯）：在用哪台 / 几帧 / 为什么不行。"""
        pub = self._pub_link
        if pub is None:
            return
        source = self._source
        current = source.current if source is not None else None
        device = current.name if current is not None else ""
        virtual = bool(current is not None and current.detail.get("virtual"))
        if source is not None and source.is_open:
            state = "ready"
            detail = f"正在采集：{device}（实测 {self._fps:.1f} FPS）"
            hint = ""
            if virtual:
                # ★ 实测踩过（用户："没有姿态数据，是不是模型压根没装？？？"）：
                #   真摄像头被别的程序占着（微信 / Edge 页里用着摄像头 / 相机 / 会议软件），
                #   自动换设备只能落到 **OBS Virtual Camera** —— 它是**占位/虚拟画面、没有人**，
                #   于是 MediaPipe 一直 present=False，面板上就是"没有姿态数据"。
                #   模型没问题（pose_landmarker 已加载），是**画面里没有人**。
                detail += "；这是虚拟摄像头（占位画面，不是真实画面）"
                hint = (
                    f"在用虚拟摄像头 {device} → 画面里没有人，姿态/人脸不会有数据。"
                    "真摄像头打不开（被别的程序占着，或本机解释器路径有问题）："
                    "关掉微信 / 浏览器里用摄像头的页面 / 相机 / OBS / 会议软件，"
                    "再在下拉里选那台真摄像头点「🔌 连接」"
                )
            elif self._last_error:
                # 采集是好的，但"上一次操作失败了"必须说出来（用户点一下没反应最气人）
                detail += f"；上一次失败：{self._last_error}"
                hint = ("设备忙 → 关掉占用摄像头的程序（OBS / 相机 / 会议软件 / 微信 / 浏览器页面），"
                        "或换一台再点「连接」；选中的那台会一直后台重试")
        elif self._errors:
            state = "error"
            detail = self._errors[-1]
            hint = "设备忙 → 关掉占用摄像头的程序（OBS/相机/会议软件）；或在下拉里换一台"
        else:
            state = "idle"
            detail = "还没打开任何摄像头"
            hint = "在下拉里选一台（本机 ACER / EOS / OBS 都能出帧）"
        pub.put({
            "ts": int(time.time() * 1000),
            "source": self.name,
            "kind": "camera",
            "state": state,
            "device": device,
            "device_id": self._device_id or "",
            "virtual": virtual,
            "fps": round(self._fps, 1),
            "target_fps": self._target_fps,
            "detail": detail,
            "hint": hint,
            "count": len(self._registry.list(CAMERA)) if self._registry else 0,
        })

    # ── 采集 ──────────────────────────────────────────────────
    def _ensure_open(self) -> bool:
        """打开（或**换一台**打开）摄像头。

        打不开时的策略见模块头：自动模式下换下一台可用设备；用户显式选的那台不行就
        保住原来在用的那台并**明确报错**，绝不偷偷替用户换走。
        """
        assert self._source is not None
        if self._source.is_open:
            return True
        try:
            self._source.open()
        except DeviceError as exc:
            self._note_error(str(exc))
            if not self._user_chose:
                if self._try_next_camera(str(exc)):
                    return True
            return False
        self._errors.clear()
        self._last_error = ""
        self._retry_index = 0
        name = self._source.current.name if self._source.current else "?"
        if self._device_id and not self._user_chose:
            self.log.info("摄像头已自动打开：%s（epoch=%d）", name, self._source.epoch)
        else:
            self.log.info("摄像头已打开：%s（epoch=%d）", name, self._source.epoch)
        self._device_id = self._source.current.id if self._source.current else self._device_id
        self.publish_link()
        return True

    def _try_next_camera(self, reason: str) -> bool:
        """自动模式下：换一台**能打开**的摄像头（失败的那台记进 errors）。"""
        assert self._source is not None and self._registry is not None
        tried = {str(self._device_id or "")}
        for device in self._registry.list(CAMERA):
            if not device.available or device.id in tried:
                continue
            tried.add(device.id)
            try:
                self._source.switch(device.id)
            except DeviceError:
                continue
            self._device_id = device.id
            self._errors.clear()
            self._last_error = ""
            self._retry_index = 0
            self.log.warning(
                "摄像头自动改用 %s（原来那台打不开：%s）", device.name, reason
            )
            self.publish_link()
            return True
        return False

    def _note_error(self, reason: str) -> None:
        """记一次失败：**退避 + 只在变化时刷日志**（原来每秒一条，把日志刷爆）。"""
        self._last_error = reason
        if not self._errors or self._errors[-1] != reason:
            self._errors.append(reason)
            self.log.warning("打开摄像头失败：%s", reason)
        else:
            self._retry_index += 1
            if self._retry_index in (3, 10, 30) or self._retry_index % 60 == 0:
                self.log.warning(
                    "打开摄像头仍然失败（第 %d 次）：%s —— 设备忙就关掉占用它的程序，"
                    "或在下拉里换一台", self._retry_index, reason,
                )
        self.publish_link()

    def _retry_delay(self) -> float:
        index = min(self._retry_index, len(_RETRY_BACKOFF_S) - 1)
        return _RETRY_BACKOFF_S[index]

    async def _capture_loop(self) -> None:
        assert self._source is not None and self._buffer is not None and self._executor is not None
        loop = asyncio.get_running_loop()
        interval = 1.0 / self._target_fps
        while True:
            if not self._source.is_open:
                # ★ 打开也放线程池：`cv2.VideoCapture(index, CAP_DSHOW)` 实测能阻塞十几秒
                #   （本机 HDI 那台），在事件循环里调等于把整个节点冻住。
                await loop.run_in_executor(self._executor, self._ensure_open)
                if not self._source.is_open:
                    await asyncio.sleep(self._retry_delay())
                    continue
            try:
                frame = await loop.run_in_executor(self._executor, self._source.read)
            except DeviceError as exc:
                self.log.warning("读帧失败：%s", exc)
                frame = None
            if frame is None:
                await asyncio.sleep(0.02)
                continue
            try:
                descriptor = self._buffer.write(
                    frame, ts_ms=int(time.time() * 1000), seq=self._seq, epoch=self._source.epoch
                )
            except Exception as exc:  # noqa: BLE001 - 写共享内存失败不该打死采集
                self.log.warning("写帧缓冲失败：%s", exc)
                descriptor = None
            if descriptor is not None:
                self._seq += 1
                self._pub.put(descriptor)
                self._count_frame()
            await asyncio.sleep(interval)

    def _count_frame(self) -> None:
        self._frames += 1
        now = time.monotonic()
        if self._last_fps_ts == 0.0:
            self._last_fps_ts = now
            return
        elapsed = now - self._last_fps_ts
        if elapsed >= 2.0:
            # 每 2s 更新一次**实测帧率**：面板要看得见"换摄像头到底有没有变化"
            self._fps = self._frames / elapsed
            # ★ 记住"这台是不是一直打不出帧"（本机内置 ACER 实测 ~1 FPS）——
            #   持续够久 `_maybe_recover` 会换一台（用户点名的只提示，不擅自换走）。
            if self._fps < _MIN_OK_FPS:
                if not self._slow_since:
                    self._slow_since = now
            else:
                self._slow_since = 0.0
            if now - self._last_fps_window >= 10.0:
                self.log.info("采集帧率 ≈ %.1f FPS（近 %d 帧）", self._fps, self._frames)
                self._last_fps_window = now
            self._frames = 0
            self._last_fps_ts = now

    @property
    def _errors(self) -> list[str]:
        """打开失败的记录（放属性里，便于测试与复用）。"""
        if not hasattr(self, "_error_list"):
            self._error_list: list[str] = []
        return self._error_list

    # ── 设备指令 ──────────────────────────────────────────────
    async def on_select(self, msg: Any) -> None:
        if not isinstance(msg, dict):
            return
        kind = msg.get("kind")
        if kind not in (None, CAMERA):
            return
        device_id = msg.get("id")
        if not device_id or self._source is None:
            return
        # ★ 记住"用户点名要的那台"：即使当下打不开（设备忙）也留着 ——
        #   `_maybe_recover()` 会每隔 `_RECOVER_EVERY_S` 秒回头试它，
        #   真摄像头一空出来就自动切过去（实测用户最气人的就是"选了没反应，之后也不会自己好"）。
        self._wanted_id = str(device_id)
        self._user_chose = True
        try:
            await self._switch_in_thread(str(device_id))     # 线程池里切，别冻结事件循环
        except DeviceError as exc:
            # ★ 明确报错 + 保留原来在用的那台（不偷偷换走用户的选择）
            current = self._source.current
            self._recover_at = time.monotonic()
            self._note_error(
                f"切到 {device_id} 失败：{exc}"
                + (f"（仍在用 {current.name}）" if current is not None else "")
            )
            self.log.warning(
                "切换摄像头失败（保持原设备，%d 秒后重试）：%s", int(_RECOVER_EVERY_S), exc
            )
            return
        self._device_id = str(device_id)
        self._user_chose = True
        self._errors.clear()
        self._last_error = ""
        self._retry_index = 0
        self._frames = 0
        self._last_fps_ts = time.monotonic()
        self.log.info(
            "摄像头已热切换到 %s（epoch=%d）—— 下游据此重建时间轴",
            self._source.current.name if self._source.current else device_id,
            self._source.epoch,
        )
        self.publish_link()


NODE = CameraNode

if __name__ == "__main__":  # pragma: no cover - 便于 `python -m agent.nodes.camera`
    raise SystemExit(CameraNode.main())
