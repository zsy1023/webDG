"""``audio`` 节点 —— 音量分级 + **语音识别**（发 ``feature/audio``）。

* **T2 部分**：音量 + 分级事件（需求 1e 的"声音有多大"）；
* **T6 部分**：语音识别 + 关键词命中（需求 1e 的"说了什么 / 是不是在求饶"）——
  用 sherpa-onnx 流式中文模型，见 :mod:`agent.nodes.audio.asr`。

- 用 ``sounddevice.InputStream`` 打开选中麦克风（按稳定 ID 重新定位 index）；
  **开 ASR 时优先 16kHz**（模型要求）；
- 回调线程只算 RMS + **把样本塞进队列**（重活绝不放进音频回调，否则丢帧/爆音）；
- 识别在**独立线程**里做：边喂边出字，端点检测到"一句说完"就定稿并匹配关键词；
- 后台任务按 ``target_hz`` 发布
  ``feature/audio = {ts, seq, level, event, text, last_text, keywords}``；
- 订阅 ``command/select_device``：``{"kind":"microphone","id":...}`` 热切换麦克风。

没有麦克风 / 没装 sounddevice / **没下 ASR 模型**时一律**优雅降级**：
只记告警，节点照常活着（不崩），`text` 保持 null。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from agent.core import Node, topics
from agent.core import devices as devices_mod
from agent.core.devices import MICROPHONE, DeviceError, DeviceRegistry

__all__ = ["AudioNode", "NODE"]

#: 音量分级阈值（RMS）：< silent 静音；< loud 普通声；否则大声
_SILENT_RMS = 0.010
_LOUD_RMS = 0.120


class AudioNode(Node):
    name = "audio"

    def __init__(
        self,
        config: Any = None,
        *,
        device_id: str | None = None,
        sample_rate: int | None = None,
        block_size: int = 1024,
        target_hz: int = 30,
        registry: DeviceRegistry | None = None,
        asr: bool = True,
        asr_threads: int = 2,
    ) -> None:
        super().__init__(config)
        self._device_id = device_id
        #: ASR 要 16k；不给就**优先 16k**（不给 ASR 用的话设备默认也行）
        self._sample_rate = sample_rate if sample_rate is not None else (16000 if asr else None)
        self._block_size = max(64, int(block_size))
        self._target_hz = max(1, int(target_hz))
        self._registry = registry
        self._want_asr = bool(asr)
        self._asr_threads = max(1, int(asr_threads))
        self._stream: Any = None
        self._worker: asyncio.Task[Any] | None = None
        self._seq = 0
        self._level = 0.0
        self._peak = 0.0

        # ── 语音识别（T6）：音频块进队列，识别在**独立线程**做 ──
        self._asr: Any = None
        self._asr_thread: Any = None
        self._asr_queue: Any = None
        self._asr_stop: Any = None
        self._text: str = ""              # 当前（未定稿的）转写
        self._last_text: str = ""         # 最近一句定稿的转写
        self._keywords: list[str] = []    # 最近一句命中的关键词
        self._asr_error: str = ""

    # ── 生命周期 ──────────────────────────────────────────────
    async def setup(self) -> None:
        if self._registry is None:
            self._registry = DeviceRegistry(logger=self.log)
        self._pub = self.publish(topics.FEATURE_AUDIO)
        self.subscribe(topics.COMMAND_SELECT_DEVICE, self.on_select)
        mics = self._registry.list(MICROPHONE)
        self.log.info("发现 %d 个麦克风：%s", len(mics), " | ".join(d.name for d in mics) or "（无）")
        self._open_stream()
        self._start_asr()
        self._worker = asyncio.create_task(self._publish_loop())

    async def teardown(self) -> None:
        task, self._worker = self._worker, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._close_stream()
        self._stop_asr()

    # ── 语音识别（T6）─────────────────────────────────────────
    def _start_asr(self) -> None:
        """起识别器 + 工作线程。**模型缺失不算错误**（降级为只做音量分级）。"""
        if not self._want_asr:
            return
        import queue
        import threading

        try:
            from agent.nodes.audio.asr import AsrSettings, StreamingAsr, find_model_dir

            model_dir = find_model_dir(self.config.models_dir)
            if model_dir is None:
                self._asr_error = f"没找到 ASR 模型（{self.config.models_dir}）"
                self.log.warning("语音识别停用 —— %s", self._asr_error)
                return
            asr = StreamingAsr(AsrSettings(model_dir=model_dir, num_threads=self._asr_threads))
            asr.load()
            if not asr.ready:
                self._asr_error = asr.error
                return
        except Exception as exc:  # noqa: BLE001
            self._asr_error = f"起识别器失败：{exc}"
            self.log.warning("语音识别停用 —— %s", self._asr_error)
            return

        self._asr = asr
        self._asr_queue = queue.Queue(maxsize=64)
        self._asr_stop = threading.Event()
        self._asr_thread = threading.Thread(
            target=self._asr_loop, name="audio-asr", daemon=True,
        )
        self._asr_thread.start()
        self.log.info("语音识别已启用（模型 %s）", model_dir.name)

    def _stop_asr(self) -> None:
        stop, self._asr_stop = self._asr_stop, None
        if stop is not None:
            stop.set()
        thread, self._asr_thread = self._asr_thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        asr, self._asr = self._asr, None
        if asr is not None:
            asr.close()

    def _asr_loop(self) -> None:
        """识别线程：从队列取音频块 → 喂识别器 → 更新文本/关键词。

        ⚠️ 这是**独立线程**（不是事件循环、更不是 sounddevice 回调）——
        识别是重活，放回调里会丢帧/爆音。
        """
        asr = self._asr
        q = self._asr_queue
        stop = self._asr_stop
        if asr is None or q is None or stop is None:
            return
        while not stop.is_set():
            try:
                block = q.get(timeout=0.2)
            except Exception:  # noqa: BLE001 - Empty
                continue
            if block is None:      # 收尾信号
                break
            try:
                text = asr.accept(block, self._sample_rate or 16000)
                self._text = text
                if asr.endpoint_hit():
                    final = asr.finish()
                    if final.strip():
                        self._last_text = final
                        self._keywords = asr.keywords_in(final)
                        if self._keywords:
                            self.log.info("🎙 识别到「%s」（命中 %s）", final, self._keywords)
                        else:
                            self.log.debug("🎙 一句说完：%s", final)
                    self._text = ""
            except Exception as exc:  # noqa: BLE001
                self.log.debug("识别线程出错（继续）：%s", exc)

    # ── 采集流 ────────────────────────────────────────────────
    def _open_stream(self) -> None:
        assert self._registry is not None
        try:
            import sounddevice as sd  # 延迟 import
        except ImportError as exc:  # pragma: no cover - 环境问题
            self.log.warning("未安装 sounddevice，音频采集停用：%s", exc)
            return
        if not self._device_id:
            resolution = self._registry.resolve(MICROPHONE)
            if resolution.device is None:
                self.log.warning("没有可用麦克风（可用 command/select_device 指定）")
                return
            self._device_id = resolution.device.id
        # 采样率：开 ASR 时**先试 16k**（模型要求）；设备不支持就退回它自己的默认值，
        # 识别侧再用软件重采样兜底（`asr.StreamingAsr.accept(..., sample_rate=实际值)`）。
        rates: list[int | None] = []
        if self._sample_rate:
            rates.append(self._sample_rate)
        rates.append(None)                     # 兜底：让设备用自己的默认采样率

        stream = None
        last_exc: BaseException | None = None
        for rate in rates:
            try:
                kwargs = devices_mod.microphone_stream_kwargs(
                    self._registry, self._device_id, samplerate=rate, channels=1
                )
            except DeviceError as exc:
                self.log.warning("麦克风不可用：%s", exc)
                return
            try:
                stream = sd.InputStream(
                    callback=self._callback,
                    blocksize=self._block_size,
                    **kwargs,
                )
                stream.start()
            except Exception as exc:  # noqa: BLE001 - 设备忙 / 采样率不支持都要降级（B8）
                last_exc = exc
                stream = None
                continue
            actual = int(kwargs["samplerate"])
            if rate is not None and actual != rate:
                self.log.info("麦克风不吃 %dHz，改用 %dHz（识别侧会重采样）", rate, actual)
            self._sample_rate = actual
            break

        if stream is None:
            self.log.warning("打开麦克风失败：%s", last_exc)
            self._stream = None
            return
        self._stream = stream
        self.log.info(
            "麦克风已打开：id=%s，采样率=%d，块=%d",
            self._device_id,
            self._sample_rate,
            self._block_size,
        )

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:  # noqa: BLE001
                self.log.debug("关闭麦克风失败：%s", exc)

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        """sounddevice 回调线程：只算音量 + **往队列塞一份**，绝不做重活、绝不抛。"""
        try:
            import numpy as np

            if indata is None or len(indata) == 0:
                return
            rms = float(np.sqrt(np.mean(np.square(indata, dtype="float64"))))
            peak = float(np.max(np.abs(indata)))
            # 语音识别要原始样本 → 拷一份进队列（丢块也不能阻塞回调）
            q = self._asr_queue
            if q is not None:
                try:
                    q.put_nowait(np.array(indata, dtype="float32").reshape(-1))
                except Exception:  # noqa: BLE001 - 队列满 = 识别跟不上，丢掉这块
                    pass
        except Exception:  # noqa: BLE001
            return
        # 指数平滑，避免音量条抖动
        self._level = 0.7 * self._level + 0.3 * rms
        self._peak = peak

    # ── 发布 ──────────────────────────────────────────────────
    async def _publish_loop(self) -> None:
        interval = 1.0 / self._target_hz
        while True:
            level = min(1.0, max(0.0, self._level))
            self._seq += 1
            self._pub.put(
                {
                    "ts": int(time.time() * 1000),
                    "seq": self._seq,
                    "level": round(level, 4),
                    "event": _classify(level),
                    # T6：语音识别（进行中的 `text` + 最近一句定稿的 `last_text` / `keywords`）
                    "text": self._text or None,
                    "last_text": self._last_text or None,
                    "keywords": list(self._keywords) or None,
                }
            )
            await asyncio.sleep(interval)

    # ── 设备指令 ──────────────────────────────────────────────
    async def on_select(self, msg: Any) -> None:
        if not isinstance(msg, dict) or msg.get("kind") not in (None, MICROPHONE):
            return
        device_id = msg.get("id")
        if not device_id:
            return
        self._device_id = str(device_id)
        self._close_stream()
        self._open_stream()
        self.log.info("麦克风已热切换到 %s", self._device_id)


def _classify(level: float) -> str:
    if level < _SILENT_RMS:
        return "silent"
    if level < _LOUD_RMS:
        return "sound"
    return "loud"


NODE = AudioNode

if __name__ == "__main__":  # pragma: no cover - 便于 `python -m agent.nodes.audio`
    raise SystemExit(AudioNode.main())
