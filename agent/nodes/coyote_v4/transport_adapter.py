"""手动传输模式：把 V4 的 **WebSocket 语义**桥接到本机的 ``command/raw_v4`` 上。

任务书 T4 §5（v3 适配版）：

> §5 的"手动传输模式"适配方式变了：不再自研 transport，而是**订阅 `command/raw_v4`**
> （由 `sim_coyote` 节点发来）

SDK 侧的事实（读 `reference/dglab-kit-python/src/dglab_kit_python/socket/base.py` 确认）：

============================  ==================================================
``DglabSocket(version=...)``  不传 ``url`` → 装一个 ``_ManualSocket``，即"手动传输模式"
``set_sender(fn)``            注册发送器：SDK 要发数据时调 ``fn(data)``，我们负责送到对端
``handle_message(data)``      收到对端数据时喂给 SDK（``str`` 或 ``bytes`` 都可以）
``handle_open()`` / ``handle_close(code, reason)``
                              手动模式下 **没有人会替你调**，连接/断开要自己通知 SDK
============================  ==================================================

因此本模块做三件事：

1. **出站**：``socket.set_sender(self._on_sdk_send)`` → 把文本帧交给注入的 ``send`` 回调
   （节点里就是 ``Publisher.put(text)``）；
2. **入站**：节点订阅到 ``command/raw_v4`` 后调 :meth:`RawV4Transport.feed` →
   ``socket.handle_message(text)``；
3. **帧原样透传**：入站只做"拿到文本"这一步，不解析再重组（任务书 §5 实现要求 2）。

⚠️ **入站方向必须走异步 handler**（基类的 ``subscribe`` 对 ``async def`` 会
``asyncio.run_coroutine_threadsafe`` 到主事件循环）。原因：``handle_message`` 会
``emit`` SDK 事件，而 SDK 的 ``EventEmitter`` 对 ``async`` 监听器用
``asyncio.get_running_loop()`` 排任务 —— 在 Zenoh 回调线程里没有运行中的 loop，
它会退化成 ``asyncio.run(...)``（在该线程另起一个临时循环），很容易踩坑。
把入站整条链路固定在事件循环线程上，就能避开这个问题。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

__all__ = ["RawV4Transport", "PayloadError"]

logger = logging.getLogger("coyotelink.coyote_v4.transport")


class PayloadError(ValueError):
    """``command/raw_v4`` 上收到的 payload 无法还原成 V4 文本帧。"""


def payload_to_text(payload: Any) -> str:
    """把订阅侧拿到的东西还原成 V4 的**文本帧**。

    - ``str`` → 原样；
    - ``bytes`` / ``bytearray`` / ``memoryview`` → UTF-8 解码；
    - ``dict`` / ``list``（基类 ``subscribe`` 会把合法 JSON 解析成对象 —— V4 帧本身
      就是 JSON 对象，所以这条最常走）→ 重新紧凑序列化（``ensure_ascii=False``）；
    - 其他 → :class:`PayloadError`。

    重新序列化只发生在"基类已经解析过"的情况下：SDK 要的是合法 JSON 文本，
    紧凑化不改变语义（键顺序保持、``ensure_ascii=False`` 保住中文）。
    """
    if isinstance(payload, str):
        return payload
    if isinstance(payload, (bytes, bytearray, memoryview)):
        try:
            return bytes(payload).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PayloadError(f"raw_v4 payload 不是合法 UTF-8：{exc}") from exc
    if isinstance(payload, (dict, list)):
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    raise PayloadError(f"raw_v4 payload 类型不支持：{type(payload).__name__}")


class RawV4Transport:
    """``command/raw_v4`` ⇄ SDK 的桥。

    构造时只需要一个"出站"回调；入站由节点订阅后调 :meth:`feed`。

    :param send: ``send(text)`` —— 把一帧 V4 文本发到对端（节点里是 ``Publisher.put``）
    :param name: 日志用名字（默认 ``coyote_v4``）
    """

    def __init__(self, send: Callable[[Any], None], *, name: str = "coyote_v4") -> None:
        if not callable(send):
            raise TypeError("send 必须可调用：send(text) → None")
        self._send = send
        self._socket: Any = None
        self._log = logging.getLogger(f"coyotelink.coyote_v4.transport.{name}")
        #: 便于测试/诊断
        self.sent = 0
        self.received = 0
        self.errors = 0

    # —— 绑定 SDK ——
    @property
    def bound(self) -> bool:
        return self._socket is not None

    def bind(self, socket: Any) -> "RawV4Transport":
        """把 SDK socket 的发送器接到本通道（幂等，可重复调用）。"""
        self._socket = socket
        socket.set_sender(self._on_sdk_send)
        return self

    # —— 出站：SDK → 对端 ——
    def _on_sdk_send(self, data: Any) -> None:
        """SDK 要发数据时回调（在事件循环线程里）。**不解析**，只做类型规整。"""
        try:
            text = payload_to_text(data)
        except PayloadError as exc:
            self.errors += 1
            self._log.error("SDK 要发的内容无法转成文本：%s", exc)
            return
        try:
            self._send(text)
        except Exception as exc:  # noqa: BLE001 - 发不出去不能把 SDK 拖崩
            self.errors += 1
            self._log.error("raw_v4 出站失败（%d 字节）：%s", len(text), exc)
            return
        self.sent += 1
        self._log.debug("raw_v4 出站 %d 字节：%s", len(text), text[:160])

    # —— 入站：对端 → SDK ——
    async def feed(self, payload: Any) -> None:
        """订阅 ``command/raw_v4`` 的 handler 调它（**async**，见模块 docstring）。"""
        if self._socket is None:
            self.errors += 1
            self._log.warning("收到 raw_v4，但还没绑定 SDK socket，丢弃：%r", payload)
            return
        try:
            text = payload_to_text(payload)
        except PayloadError as exc:
            self.errors += 1
            self._log.warning("raw_v4 入站 payload 无法还原成文本：%s", exc)
            return
        self.received += 1
        self._log.debug("raw_v4 入站 %d 字节：%s", len(text), text[:160])
        self._socket.handle_message(text)

    # —— 连接状态（手动模式下得自己通知 SDK）——
    def mark_open(self, event: Any = None) -> None:
        """对端链路已建立（手动模式下 SDK 收不到 WebSocket 的 open）。"""
        if self._socket is not None:
            self._socket.handle_open(event)

    def mark_closed(self, code: int = 1000, reason: str = "raw_v4 channel closed") -> None:
        """对端链路断开 → 让 SDK 状态机正确迁移（任务书 §5 实现要求 3）。"""
        if self._socket is not None:
            self._log.info("raw_v4 链路关闭（code=%s, reason=%s）", code, reason)
            self._socket.handle_close(code, reason)
