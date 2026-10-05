"""``web`` 节点 —— 面板后端（T5）。

职责
----
1. 订阅 ``coyotelink/<room>/**``（``topics.all_keys``）—— 全部 topic；
2. 把每条 Zenoh 消息**翻译**成浏览器直接可用的 JSON，经 ``/ws`` 广播；
3. 接收浏览器的指令（开火 / 急停 / 选设备 / 波形导入），翻译成对应的
   ``command/*`` 发回 Zenoh（设备枚举与波形校验的权威点在别的节点 / ``waveform_io``）。

⚠️ **``/ws`` 握手 403 的真因（2026-10 修正过旧结论）**：**不是**启动方式 —— 是**注解解析**。
    本文件启用了 ``from __future__ import annotations``，注解会延迟成字符串；FastAPI 用
    ``get_type_hints()`` 解析端点签名，而它**只认模块 globals**。若 ``WebSocket`` 只在
    ``build_app()`` 内部导入，``ws: WebSocket`` 会退化成 ``ForwardRef('WebSocket')`` →
    被当成"必需的 query 参数" → 依赖校验失败 → 端点未执行就 close → **所有 /ws 403**
    （HTTP 路由却正常，所以极难排查）。因此 ``fastapi`` 的导入**必须留在模块顶层**。

⚠️ 启动仍请用 ``uvicorn.run(app, ...)``（它自己掌管事件循环，见下一条）。

⚠️ **本节点仍继承** :class:`agent.core.Node`（订阅 / 发布都走基类），但**不**通过
    ``asyncio.run(node.run())`` 驱动 —— 因为 ``uvicorn.run()`` 自己掌管事件循环。
    于是把 Node 的生命周期挂在 FastAPI 的 lifespan 上：
    ``bind_to_loop()`` 复刻 ``run()`` 的初始化（拿 loop → 开 session → ``setup()``），
    退出时 ``unbind()`` 调 ``_shutdown()``（撤订阅、关 session，不留残留）。

隐私（需求 1d）：只搬结构化 JSON；``feature/frame.raw`` 的原始像素从未进入本节点。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import re
import socket
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from agent.core import Node, topics
from agent.core.config import Config, force_utf8

#: ⚠️ fastapi 的导入**必须在模块顶层**（不能用函数内懒加载）。
#: 本文件启用了 ``from __future__ import annotations``，所有注解会被延迟成字符串；
#: FastAPI 用 ``get_type_hints()`` 解析端点签名，而它**只认模块 globals**。
#: 若 ``WebSocket`` 是在 ``build_app()`` 内部导入的，``ws: WebSocket`` 会退化成
#: ``ForwardRef('WebSocket')`` → FastAPI 把它当成**必需的 query 参数** → 依赖校验失败 →
#: 端点还没执行就 ``close()`` → 所有 ``/ws`` 握手 **403**（HTTP 路由却正常，极难排查）。
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

__all__ = ["WebNode", "NODE", "Hub"]

#: 前端静态资源根目录：<项目根>/web
_WEB_ROOT = Path(__file__).resolve().parents[3] / "web"
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8780

#: 文件名里不允许的字符（波形落盘用）
_BAD_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

#: 房间码：与 `web/js/room.js` 的 RE_CODE 一致（大写字母 / 数字，4~16 位）。
#: 也兼容 relay 自己生成的那种（`secrets.token_hex(3).upper()`）。
#: 房间码形状：**≥6 位**的大写字母/数字，且**不能纯数字**
#: （纯数字几乎必然是用户把樱花端口号填进了房间码框 —— 实测把 `65230` 当房间码，
#:  结果两端房间不一致、跨机链路整个连不上。前端 `web/js/room.js` 有同一套校验。）
_ROOM_CODE = re.compile(r"^[A-Z0-9]{6,16}$")
_ROOM_CODE_DIGITS = re.compile(r"^[0-9]+$")


def _room_code_error(code: str) -> str | None:
    """房间码不合法 → 返回给面板的中文原因；合法 → ``None``。"""
    if _ROOM_CODE_DIGITS.match(code):
        return "房间码不能是纯数字 —— 这看着像端口号；樱花端口请填在「穿透地址 / 端口」那一栏"
    if not _ROOM_CODE.match(code):
        return "房间码只能是大写字母 / 数字，6~16 位（例如 AB12CD）"
    return None

#: 待发队列上限（重要消息；满了丢最老的）—— 见 `Hub.broadcast` 的丢包说明
_SEND_QUEUE_MAX = 400
#: 下发节流间隔（秒）：一批最多这么快发一次，高频帧只在批里留最新
_SEND_INTERVAL_S = 1 / 30

#: 单个 WS 客户端单条消息的发送超时（秒）。**必须有**：否则一个被后台冻结 / 断连却没报错的
#: 浏览器会让 `Hub._drain` 永久卡住，之后骨骼、表情、链路状态统统不再下发
#: （2026-10-05 实测症状：连上瞬间能看到 `last` 快照，之后全静止，而 zenoh 层还在满速发）。
_SEND_TIMEOUT_S = 0.2


def _slug(name: str) -> str:
    """把波形名变成安全的文件名（保中文，替换非法字符）。"""
    cleaned = _BAD_FILENAME.sub("_", str(name)).strip().strip(".")
    return cleaned or "waveform"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _decode(msg: Any) -> Any:
    """Node 基类可能给 dict（正常）也可能给 bytes（JSON 解不开）—— 尽量还原成 dict。"""
    if isinstance(msg, dict):
        return msg
    if isinstance(msg, (bytes, bytearray, memoryview)):
        try:
            return json.loads(bytes(msg).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
    return None


class Hub:
    """广播中心：Zenoh 回调线程 → asyncio 事件循环 → 所有 WS 客户端。

    与 ``web/pose-preview/server.py`` 的 Hub 同思路：
    Zenoh 的订阅回调跑在**非主线程**，不能直接 ``await ws.send_text``；
    必须 ``run_coroutine_threadsafe`` 桥接到 uvicorn 的 loop。
    """

    #: 这几类高频消息**不进** ``last`` 快照（否则新连入的浏览器会先收到一帧旧姿态），
    #: 而且下发时**只留最新一份**（旧帧没人要 —— 前端只渲染最新帧）
    _EPHEMERAL = frozenset({"pose", "face", "audio", "heart"})

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[Any] = set()
        self.last: dict[str, Any] = {}
        #: 高频消息"只留最新一份"（旧帧没人要 —— 前端本来就只渲染最新帧）
        self._pending: dict[str, str] = {}
        #: 低频但重要的消息（链路状态/开火回执/波形/房间…）走有序队列，一条都不许丢
        self._queue: deque[str] = deque(maxlen=_SEND_QUEUE_MAX)
        self._draining = False
        self._dropped = 0

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def close(self) -> None:
        self.loop = None
        self.clients.clear()
        self._pending.clear()
        self._queue.clear()

    async def register(self, ws: Any) -> None:
        await ws.accept()
        self.clients.add(ws)
        # 补齐"最近一条"各类型消息，让刚连上的浏览器不用干等
        for payload in list(self.last.values()):
            with contextlib.suppress(Exception):
                await ws.send_text(json.dumps(payload, ensure_ascii=False))

    def unregister(self, ws: Any) -> None:
        self.clients.discard(ws)

    def broadcast(self, payload: dict[str, Any]) -> None:
        """zenoh 回调线程 → 事件循环。**同步、不建协程、不阻塞**（见下面这行长注释）。

        ⚠️ 老实现是"每条消息都 ``run_coroutine_threadsafe(_send_all)``"：
        面板 WS 实测 130 条/秒 → 每秒 130 个协程排队 await 浏览器 socket；
        浏览器一慢，协程就堆在 loop 上 → zenoh 回调线程跟着堵 → 默认
        ``congestion_control=DROP`` 开始丢包 → **开火 / 规则 / 断线事件随机丢失**
        （用户看到的就是"点了没反应""状态不更新"）。
        现在：高频帧只留最新（``_pending``），其余进有序队列（``_queue``），
        由**唯一的** ``_drain`` 任务按帧节流下发 —— 每秒最多一个协程；
        浏览器慢只会丢旧帧，指令与状态一条不丢。
        """
        kind = str(payload.get("type", "?"))
        if kind not in self._EPHEMERAL:
            self.last[kind] = payload
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        try:
            text = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            return
        if kind in self._EPHEMERAL:
            self._pending[kind] = text          # 只留最新一帧
        else:
            if len(self._queue) == self._queue.maxlen:
                self._dropped += 1              # 队列满 → 丢最老的（宁可丢旧状态，也不堵住）
                self.last["_dropped"] = {"type": "dropped", "count": self._dropped}
            self._queue.append(text)
        try:
            loop.call_soon_threadsafe(self._ensure_drain)
        except RuntimeError:                    # 事件循环正在关闭
            return

    def _ensure_drain(self) -> None:
        """（在事件循环线程里跑）保证只有一个 `_drain` 任务在干活。"""
        if self._draining or self.loop is None or self.loop.is_closed():
            return
        self._draining = True
        self.loop.create_task(self._drain())

    async def _drain(self) -> None:
        try:
            while self._queue or self._pending:
                batch = list(self._queue)
                self._queue.clear()
                batch.extend(self._pending.values())
                self._pending.clear()
                for text in batch:
                    await self._send_all(text)
                await asyncio.sleep(_SEND_INTERVAL_S)   # 帧级节流：最多 ~30 批/秒
        finally:
            self._draining = False

    async def _send_all(self, text: str) -> None:
        dead: list[Any] = []
        for ws in list(self.clients):
            try:
                # ⚠️ 必须带超时（2026-10-05 实测踩过）：浏览器标签被后台冻结、或网线被拔时，
                #    `ws.send_text` 会一直 await 不返回 → 整个 `_drain` 永久卡在这里 →
                #    `_draining` 永远是 True → **面板此后一条都不再下发**。
                #    症状：刚连上能看到 register() 里 `last` 快照那几条（hello/link/设备列表），
                #    之后骨骼、表情、状态全静止 —— 而 zenoh 层 pose/audio 明明 100 条/10 秒在发。
                await asyncio.wait_for(ws.send_text(text), timeout=_SEND_TIMEOUT_S)
            except Exception:  # noqa: BLE001 - 断开的客户端 / 发送超时
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


class WebNode(Node):
    """面板后端节点。"""

    name = "web"
    #: 面板不发 status 心跳（它本身是"观察者"，没必要占用 status key）
    heartbeat_interval = 0.0
    #: 终止时 teardown 超时
    teardown_timeout = 5.0

    def __init__(
        self,
        config: Config | None = None,
        *,
        host: str = _DEFAULT_HOST,
        port: int = _DEFAULT_PORT,
        web_root: Path | None = None,
    ) -> None:
        super().__init__(config)
        self.host = host
        self.port = int(port)
        self.web_root = Path(web_root) if web_root is not None else _WEB_ROOT
        self._hub = Hub()
        self._wave_dir = self.config.data_dir / "waveforms"
        self._pub_fire: Any = None
        self._pub_clear: Any = None
        self._pub_select: Any = None
        self._pub_rules: Any = None
        self._pub_room: Any = None      # command/room（换房间：全体节点跟着换 key 前缀）
        #: coyote_v4 报上来的 targetId（配对二维码里有它；变了就重推配对信息）
        self._relay_target_id: str = ""
        self._frame_seq_gap = 0  # 统计用（丢帧计数）

    # ──────────────────────────────────────────────────────────
    # Node 生命周期（setup / teardown）
    # ──────────────────────────────────────────────────────────
    async def setup(self) -> None:
        self._pub_fire = self.publish(topics.COMMAND_FIRE)
        self._pub_clear = self.publish(topics.COMMAND_CLEAR)
        self._pub_select = self.publish(topics.COMMAND_SELECT_DEVICE)
        self._pub_rules = self.publish(topics.COMMAND_RULES)
        self._pub_room = self.publish(topics.COMMAND_ROOM)
        # ★ **别订 `**`**：那会把 `bridge/<对端>/...` 的**跨机 AEAD 密文副本**也捞进这个
        #   进程 —— 它们对面板毫无用处（同一份数据的面文会另有一条明文 key 到达），
        #   却会让 web 节点：① 每条都"解不开"刷一条 WARNING（日志爆炸）② 转发给浏览器
        #   （实测面板 WS 每秒 132 条里有 ~70 条是这种垃圾，真实消息还被**传了两遍**）。
        #   实测后果：机器一忙，zenoh 就按默认的 congestion_control=DROP 丢包 ——
        #   面板发的开火/规则/导入指令、以及"手机断开"这类事件**随机收不到**，
        #   用户看到的就是"点了没反应/状态不更新"。只订下面这 5 类，垃圾根本不进进程。
        # ⚠️ 这里必须传**后缀**（`topics.FEATURE_ALL` 这类常量），不能传
        #   `topics.feature_keys()` —— 后者是**已经拼好 room 的完整 key**（默认 room='local'），
        #   会让节点订到 `coyotelink/local/...` 上去（基类会警告"room 不一致"，但数据也就没了）。
        for suffix in (
            topics.FEATURE_ALL,
            topics.EVENT_ALL,
            topics.COMMAND_ALL,
            topics.STATUS_ALL,
            topics.TEST_ALL,
        ):
            self.subscribe(suffix, self.on_sample, with_key=True)
        self.log.info("web 面板后端订阅：%s（**不含 bridge 密文副本**）", topics.all_keys(self.room))

    async def teardown(self) -> None:
        self._hub.close()

    # ──────────────────────────────────────────────────────────
    # uvicorn 生命周期桥接（★ 因为启动必须用 uvicorn.run，见文件头）
    # ──────────────────────────────────────────────────────────
    async def bind_to_loop(self) -> None:
        """在 uvicorn 的 loop 里初始化：复刻 ``Node.run()`` 的关键步骤。"""
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._hub.attach(self._loop)
        self._open_session()          # 打开 Zenoh session（基类方法）
        try:
            self._subscribe_room_command()   # ★ 房间指令：web 自己也得跟着换
            await self.setup()
        except BaseException as exc:  # noqa: BLE001 - 先记日志（handler 还开着）再抛
            self._fatal(exc, where="setup")
            raise
        finally:
            self._started.set()
        self.log.info("web 面板就绪：http://%s:%d（room=%s）", self.host, self.port, self.room)

    async def unbind(self) -> None:
        """uvicorn 退出时收尾（撤订阅、关 session）。"""
        await self._shutdown()

    # ──────────────────────────────────────────────────────────
    # Zenoh → 浏览器
    # ──────────────────────────────────────────────────────────
    def on_sample(self, key: str, msg: Any) -> None:
        """订阅回调（**非主线程**）：翻译一条消息并广播。"""
        try:
            suffix = topics.suffix_of(key)
        except Exception:  # noqa: BLE001
            suffix = str(key)
        # `feature/frame.raw` 是**本机共享内存的指路牌**（给 pose/face 两个进程用的），
        # 浏览器拿不到那块内存、也用不上 —— 30 帧/秒白转发，直接丢。
        if suffix == topics.FEATURE_FRAME_RAW:
            return
        payload = self._translate(suffix, msg)
        if payload is None:
            return
        if self.log.isEnabledFor(logging.DEBUG):
            kind = payload.get("kind")
            self.log.debug("→ 面板 type=%s%s", payload.get("type"), f" kind={kind}" if kind else "")
        self._hub.broadcast(payload)

    def _translate(self, suffix: str, msg: Any) -> dict[str, Any] | None:
        if suffix == topics.FEATURE_POSE:
            return self._pose_payload(msg)
        if suffix == topics.FEATURE_FACE:
            data = _decode(msg)
            if not isinstance(data, dict):
                return None
            data["type"] = "face"
            data.setdefault("recv", _now_ms())
            return data
        if suffix == topics.FEATURE_AUDIO:
            data = _decode(msg)
            if not isinstance(data, dict):
                return None
            data["type"] = "audio"
            return data
        if suffix == topics.FEATURE_HEART:
            data = _decode(msg)
            if not isinstance(data, dict):
                return None
            data["type"] = "heart"
            return data
        if suffix == topics.EVENT_PAWPRINT:
            data = _decode(msg)
            if not isinstance(data, dict):
                return None
            data["type"] = "pawprint"
            return data
        if suffix == topics.EVENT_DEVICE:
            data = _decode(msg)
            if not isinstance(data, dict):
                return None
            data["type"] = "devices"
            return data
        if suffix == topics.EVENT_FIRE:
            data = _decode(msg)
            if not isinstance(data, dict):
                return None
            data["type"] = "fire"
            data.setdefault("recv", _now_ms())
            return data
        if suffix == topics.EVENT_LINK:
            # 链路状态（Relay / ESP32 串口 / 手机 App / 摄像头）—— 摊平进 type=link，
            # 前端不用再去认 payload 套娃。
            data = _decode(msg)
            out: dict[str, Any] = {"type": "link", "recv": _now_ms()}
            if isinstance(data, dict):
                out.update(data)
                self._note_relay_target(data)
            return out
        if suffix.startswith(topics.STATUS_PREFIX):
            data = _decode(msg)
            node_name = suffix.split("/", 1)[1] if "/" in suffix else suffix
            return {"type": "status", "node": node_name, "payload": data, "recv": _now_ms()}
        # 其它 key：原样透传（前端"调试"用）
        return {"type": "topic", "key": suffix, "payload": _decode(msg)}

    def _pose_payload(self, msg: Any) -> dict[str, Any] | None:
        """``feature/pose`` 是紧凑编码（参考 :mod:`agent.nodes.pose.frame_codec`），此处解码。

        ⚠️ 实测坑（2026-10-05，就是这么"前端永远没有骨骼"的）：
        基类给订阅回调的 `msg` **可能已经是 dict**（它先解过一次 JSON），而
        ``FeatureFrame.from_wire`` 要的是 **payload 字节**。原来直接把 `msg` 塞进去 →
        真实链路上**每帧都解不开、被 except 静默丢弃**。证据：zenoh 层数
        `coyotelink/<room>/feature/pose` 是 100 条/10 秒（每帧 present=True、33 个关键点），
        而面板一条都收不到 → 前端骨骼永远是空的。
        所以这里统一先 `_decode`（dict → 重新序列化成字节），与其它 handler 保持一致。
        """
        raw: bytes | None = None
        if isinstance(msg, (bytes, bytearray, memoryview)):
            raw = bytes(msg)
        else:
            data = _decode(msg)
            if isinstance(data, dict):
                try:
                    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
                except (TypeError, ValueError):
                    return None
        if not raw:
            # 诊断：这条 WARNING 只在解不开时打，且限流 —— 它必须能自己说明原因，
            # 而不是像以前那样静默丢帧（"前端没有骨骼"最难查的就是这种静默）。
            self._pose_drop = getattr(self, "_pose_drop", 0) + 1
            if self._pose_drop <= 3 or self._pose_drop % 200 == 0:
                self.log.warning(
                    "pose 帧无法解码，已丢弃（第 %d 次）：msg 类型=%s，内容前 120=%r",
                    self._pose_drop, type(msg).__name__, str(msg)[:120],
                )
            return None
        # 诊断：头一帧把"基类实际给了什么"打出来一次（排查 present 恒为 False 这类静默错误）
        if not getattr(self, "_pose_raw_logged", False):
            self._pose_raw_logged = True
            self.log.info(
                "pose 原始输入：msg 类型=%s，raw 长度=%d，前 120 字节=%r",
                type(msg).__name__, len(raw), raw[:120],
            )
        try:
            from agent.nodes.pose.frame_codec import FeatureFrame

            frame = FeatureFrame.from_wire(raw)
        except Exception as exc:  # noqa: BLE001 - 解不开就丢这一帧（不打死节点）
            self._pose_drop = getattr(self, "_pose_drop", 0) + 1
            if self._pose_drop <= 3 or self._pose_drop % 200 == 0:
                self.log.warning("pose 帧解码失败，已丢弃（第 %d 次）：%s", self._pose_drop, exc)
            return None
        # 诊断：解出来的帧若一直没有关键点，也打一次（这是"有帧但没人"的静默错误）
        if not frame.present and not getattr(self, "_pose_empty_logged", False):
            self._pose_empty_logged = True
            self.log.warning(
                "pose 帧解出来了但 present=False（landmarks=%d）—— 若不是真没人，"
                "就是输入字节与编码器不匹配：raw 前 120=%r",
                len(frame.landmarks or []), raw[:120],
            )
        return {
            "type": "pose",
            "ts": frame.ts,
            "seq": frame.seq,
            "present": frame.present,
            "landmarks": frame.landmarks,
            "visibility": frame.visibility,
            "recv": _now_ms(),
        }

    # ──────────────────────────────────────────────────────────
    # 浏览器 → Zenoh
    # ──────────────────────────────────────────────────────────
    def publish_fire(self, body: dict[str, Any]) -> None:
        if self._pub_fire is not None:
            self._pub_fire.put(body)
            # ★ 带 key：面板「开火」没反应时，直接对比两边日志里的完整 key 就能定位
            self.log.info("→ %s %s", self._pub_fire.key, body)

    def publish_clear(self) -> None:
        if self._pub_clear is not None:
            self._pub_clear.put({"op": "clear", "source": "web"})
            self.log.info("→ command/clear（急停）")

    def publish_select(self, kind: str, device_id: str | None, *, action: str = "select") -> None:
        if self._pub_select is None:
            return
        body: dict[str, Any] = {"action": action}
        if action == "select" and kind and device_id:
            body["kind"] = kind
            body["id"] = device_id
        self._pub_select.put(body)

    # ──────────────────────────────────────────────────────────
    # 郊狼配对（DG-LAB App 扫的二维码）
    # ──────────────────────────────────────────────────────────
    def _v4_url(self) -> str:
        """`[coyote_v4] url` —— 对端 relay 地址（含 `?room=`）。"""
        from agent.nodes.coyote_v4 import _url_from_config

        return str(_url_from_config(self.config) or "")

    def _note_relay_target(self, data: dict[str, Any]) -> None:
        """盯住 relay 状态里的 `target_id`；变了就**主动推**一次配对信息。

        为什么：配对二维码的内容就是 `…?tid=<targetId>` —— App 扫的那个码必须跟着
        targetId 变（relay 重启/重连都会换）。以前只有开发页 `web/coyote-lab/` 会显示它，
        主面板一个字都没有，用户拿不到码就连不上（实测反馈："郊狼的 socket 没有码怎么连"）。
        """
        if str(data.get("kind") or "") != "relay":
            return
        tid = str(data.get("target_id") or "")
        if not tid or tid == self._relay_target_id:
            return
        self._relay_target_id = tid
        self.log.info("拿到新的 targetId：%s → 面板可出示配对二维码", tid)
        self._hub.broadcast(self.pairing_payload())

    def pairing_payload(self) -> dict[str, Any]:
        """给面板的**郊狼配对信息**：App 该扫的二维码 + 它要连的 ws 地址。

        走 `agent.nodes.coyote_v4.pairing`（官方 SDK 的规则：二维码里装的是
        `https://dungeon-lab.cn/s/?v=1&action=socket&url=wss://…/?tid=…`）。
        """
        from agent.nodes.coyote_v4 import pairing as coyote_pairing

        url = self._v4_url()
        tid = self._relay_target_id
        info: dict[str, Any] = {
            "type": "pairing",
            "url": url,
            "target_id": tid,
            "ts": _now_ms(),
        }
        if not url:
            info["hint"] = (
                "还没配 [coyote_v4] url —— 双击 start.cmd 会自动起一个本机中继并写好地址，"
                "或手动填 agent/config.toml"
            )
            return info
        # ★ 手机要连的是**局域网地址**，不是配置里那个 127.0.0.1（那是本机自己连自己用的）。
        #   实测踩过：二维码里带 127.0.0.1，手机扫了去连"手机自己" → 永远连不上。
        # ★ 跨网/外网时更进一步：如果配了 `[coyote_v4] public_url`（樱花 HTTPS 隧道地址），
        #   就用它 —— 手机不在同一个 WiFi 也能扫（走的 wss，证书由樱花提供）。
        public = str((self.config.raw.get("coyote_v4") or {}).get("public_url") or "").strip()
        if public:
            phone_url = public
            info["public"] = True
        else:
            phone_url = coyote_pairing.reachable_url(url)
            info["public"] = False
        info["phone_url"] = phone_url
        info["lan_ip"] = coyote_pairing.lan_ip()
        info["reachable"] = phone_url != url
        if not tid:
            info["hint"] = f"还没拿到 targetId（等 coyote_v4 连上 {url}；上面那盏 Relay 灯先变绿）"
            return info
        try:
            info["ws_url"] = coyote_pairing.app_socket_url(tid, phone_url)
            info["qr_link"] = coyote_pairing.pairing_link(tid, server_url=phone_url)
            if info["reachable"]:
                info["hint"] = (
                    f"手机 DG-LAB App 扫这个码 → 会连到 {phone_url}"
                    "（手机要和电脑在**同一个 WiFi/局域网**；连不上多半是防火墙没放行 9997）"
                )
            else:
                info["hint"] = (
                    f"⚠️ 这个地址是回环地址（{url}），**手机连不上**："
                    "把 [coyote_v4] url 改成电脑的局域网 IP（面板下一步会给出建议地址），"
                    "或让 start.cmd 自动处理"
                )
        except Exception as exc:  # noqa: BLE001 - 配对信息算不出来不该打死面板
            info["error"] = f"生成配对链接失败：{exc}"
        return info

    async def handle_client(self, ws: Any, msg: Any) -> None:
        """处理一条来自浏览器的指令。"""
        if not isinstance(msg, dict):
            return
        op = str(msg.get("op") or msg.get("type") or "")
        if op == "fire":
            self.publish_fire(self._fire_body(msg))
            await self._reply(ws, {"type": "ack", "op": "fire", "ok": True})
        elif op == "clear":
            self.publish_clear()
            await self._reply(ws, {"type": "ack", "op": "clear", "ok": True})
        elif op in ("select", "select_device"):
            self.publish_select(str(msg.get("kind") or ""), msg.get("id"))
        elif op in ("list_devices", "devices"):
            self.publish_select("", None, action="list")
        elif op == "waveforms":
            await self._reply(ws, {"type": "waveforms", "waveforms": self.list_waveforms()})
        elif op == "import_waveform":
            await self._handle_import(ws, msg)
        elif op == "delete_waveform":
            await self._handle_delete(ws, msg)
        elif op == "rename_waveform":
            await self._handle_rename(ws, msg)
        elif op == "set_rules":
            await self._handle_set_rules(ws, msg)
        elif op == "room":
            await self._handle_room(ws, msg)
        elif op == "pairing":
            await self._reply(ws, self.pairing_payload())
        elif op == "ping":
            await self._reply(ws, {"type": "pong", "ts": _now_ms()})

    async def _handle_room(self, ws: Any, msg: dict[str, Any]) -> None:
        """``op:"room"`` —— 面板上的创建 / 加入 / 离开房间。

        房间就是 Zenoh 的 key 前缀（`topics.PREFIX` + room），所以"换房间"**不是**
        web 节点自己改个变量：它往 ``command/room`` 发一条，**每个节点**（含 web 自己）
        都在基类里订阅它，各自重开 session、按新前缀重声明 pub/sub（`Node.switch_room`）。

        契约（与 ``web/js/room.js`` 商定）::

            上行 { op:"room", action:"create"|"join"|"leave", code:"AB12CD", url:"ws://…" }
            下行 { type:"room", ok:true, action:…, code:…, url:… }
                 { type:"room", ok:false, action:…, error:"…" }

        `url`（对端地址）这里只**记账放行**：它是 V4 relay 的地址，由 ``coyote_v4``
        从 `agent/config.toml` 读，不在 zenoh 键空间里 —— 面板填了就当参考信息回显。
        """
        action = str(msg.get("action") or "").strip().lower()
        if action not in ("create", "join", "leave"):
            await self._reply(ws, {
                "type": "room", "ok": False, "action": action,
                "error": f"不认识的房间动作：{msg.get('action')!r}（只认 create / join / leave）",
            })
            return
        code = topics.DEFAULT_ROOM if action == "leave" else str(msg.get("code") or "").strip().upper()
        # `leave` 用的是我们自己那个默认房间名（`local`，小写）—— 不走"房间码"那套格式校验
        code_err = None if action == "leave" else _room_code_error(code)
        if code_err:
            await self._reply(ws, {
                "type": "room", "ok": False, "action": action, "code": code,
                "error": code_err,
            })
            return
        url = str(msg.get("url") or "").strip()
        body: dict[str, Any] = {"action": action, "code": code, "source": self.name}
        if url:
            body["url"] = url
        if self._pub_room is not None:
            self._pub_room.put(body)
        self.log.info("面板要求换房间：%s → %s（广播 command/room）", action, code)
        # 先回执：真正的切换是各节点收到 command/room 之后各自做的（含本节点）
        await self._reply(ws, {"type": "room", "ok": True, "action": action, "code": code,
                               "url": url, "from": self.room})

    async def on_room_switched(self, old_room: str, new_room: str) -> None:
        """换完房间：给所有浏览器重报一次 ``hello``（面板上"后端实际"要跟着变）。

        另外把"最近一条"快照里**旧房间**的消息清掉 —— 否则新连入的浏览器会先看到
        上一房间的姿态/设备列表。
        """
        hub = self._hub
        for kind in list(hub.last):
            if kind not in ("hello", "waveforms"):
                hub.last.pop(kind, None)
        hub.broadcast({"type": "hello", "room": new_room, "ts": _now_ms(),
                       "prev_room": old_room, "switched": True})
        # 新房间里重新拉一次设备快照（device 节点刚换了前缀，列表得重新推）
        self.publish_select("", None, action="list")

    async def _handle_set_rules(self, ws: Any, msg: dict[str, Any]) -> None:
        """``set_rules`` —— 把面板上配好的规则**整体**下发给 rules 节点。

        契约（与前端 ``web/js/rules.js`` 商定）：payload ``{rules:[…]}``，
        语义是**整体替换**。这里只做形状校验 + 转发；规则本身合不合法由 rules 节点判定
        （它会记 ``errors`` 并跳过坏规则，不会因为一条写错就全废）。
        """
        rules = msg.get("rules")
        if not isinstance(rules, list):
            await self._reply(ws, {"type": "ack", "op": "set_rules", "ok": False,
                                   "error": "rules 必须是数组"})
            return
        publisher = self._pub_rules
        if publisher is None:
            await self._reply(ws, {"type": "ack", "op": "set_rules", "ok": False,
                                   "error": "本节点没有规则发布者"})
            return
        publisher.put({"rules": rules})
        await self._reply(ws, {"type": "ack", "op": "set_rules", "ok": True,
                               "count": len(rules)})

    @staticmethod
    def _fire_body(msg: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "op": "fire",
            "source": "web",
            "waveform": str(msg.get("waveform") or "PULSE"),
            "channel": str(msg.get("channel") or "A").upper()[:1] or "A",
            "duration_ms": int(msg.get("duration_ms") or 2000),
            "priority": int(msg.get("priority") or 1),
            # ★ 默认**抢占**（不叠加）：叠加会让对方的波形和我们的叠在一起（实测反馈）。
            "overlay": bool(msg.get("overlay", False)),
        }
        # ★ 打哪台机器上连着的郊狼：不填/`local` = 只打本机；`all` = 房间里全打；
        #   也可以填对端主机名（面板的「开火目标」下拉就是从 status 心跳里认出来的）。
        target = str(msg.get("target") or "").strip()
        if target:
            body["target"] = target
        # ★ 谁发的：被控端视图要显示"哪个机器在控制我"
        body["by"] = socket.gethostname()
        # 强度：增加（delta）/ 设定到（abs）/ 不动（none）
        mode = str(msg.get("intensity_mode") or "")
        if msg.get("intensity_delta") is not None:
            body["intensity_delta"] = int(msg["intensity_delta"])
        elif msg.get("intensity") is not None:
            body["intensity"] = int(msg["intensity"])
        elif mode == "delta":
            body["intensity_delta"] = int(msg.get("intensity_value") or 0)
        elif mode == "abs":
            body["intensity"] = int(msg.get("intensity_value") or 0)
        return body

    async def _reply(self, ws: Any, payload: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await ws.send_text(json.dumps(payload, ensure_ascii=False))

    # ──────────────────────────────────────────────────────────
    # 波形：官方 + 导入（权威校验在 waveform_io）
    # ──────────────────────────────────────────────────────────
    def _waveform_io(self) -> Any:
        from agent.nodes.coyote_v4 import waveform_io

        return waveform_io

    def _table(self) -> dict[str, Any]:
        waveform_io = self._waveform_io()
        official: dict[str, Any] = {}
        try:
            official = waveform_io.official_waveforms()
        except Exception as exc:  # noqa: BLE001 - 未装官方 SDK 也能用（只列导入）
            self.log.warning("官方波形不可用（%s），仅列导入波形", exc)
        return waveform_io.load_all(self._wave_dir, official=official)

    def list_waveforms(self) -> list[dict[str, Any]]:
        try:
            table = self._table()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("加载波形失败：%s", exc)
            return []
        rows = [
            {
                "name": w.name,
                "source": w.source,
                "version": w.version,
                "frames": len(w.frames),
            }
            for w in table.values()
        ]
        # 官方在前，其余按名字排
        rows.sort(key=lambda r: (r["source"] != "official", r["name"]))
        return rows

    async def _handle_import(self, ws: Any, msg: dict[str, Any]) -> None:
        name = str(msg.get("name") or "").strip()
        frames = msg.get("frames")
        text = msg.get("text")
        waveform_io = self._waveform_io()
        try:
            if not name:
                raise waveform_io.WaveformError("波形名不能为空")
            if frames is None and text is not None:
                wave = waveform_io.import_text(str(text), name)  # 逐行（带行号）校验
                frames = wave.frames
            if not isinstance(frames, list) or not frames:
                raise waveform_io.WaveformError("没有任何帧")
            clean = waveform_io.normalize_frames(frames)   # 逐帧 hex 校验（带帧号）
            version = waveform_io.detect_version(clean)    # 帧长一致 + 6/16（带帧号）
            wave = waveform_io.Waveform(name=name, frames=clean, version=version, source="imported")
        except Exception as exc:  # noqa: BLE001 - 报错要精确（含帧号）
            await self._reply(ws, {"type": "import_error", "error": str(exc)})
            return
        try:
            self._wave_dir.mkdir(parents=True, exist_ok=True)
            path = self._wave_dir / f"{_slug(wave.name)}.json"
            path.write_text(
                json.dumps(
                    {"name": wave.name, "version": wave.version, "frames": wave.frames},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            await self._reply(ws, {"type": "import_error", "error": f"写入失败：{exc}"})
            return
        self.log.info("导入波形 %s（V%d，%d 帧）→ %s", wave.name, wave.version, len(wave.frames), path)
        await self._reply(
            ws,
            {
                "type": "waveforms",
                "imported": {"name": wave.name, "version": wave.version, "frames": len(wave.frames)},
                "waveforms": self.list_waveforms(),
            },
        )

    async def _handle_delete(self, ws: Any, msg: dict[str, Any]) -> None:
        name = str(msg.get("name") or "").strip()
        waveform_io = self._waveform_io()
        table = self._table()
        wave = waveform_io.find(table, name)
        if wave is None or wave.source == "official":
            await self._reply(ws, {"type": "import_error", "error": "官方波形不可删除"})
            return
        removed = False
        for candidate in {name, wave.name}:
            path = self._wave_dir / f"{_slug(candidate)}.json"
            if path.is_file():
                with contextlib.suppress(OSError):
                    path.unlink()
                    removed = True
        await self._reply(
            ws,
            {"type": "waveforms", "deleted": wave.name, "ok": removed, "waveforms": self.list_waveforms()},
        )

    async def _handle_rename(self, ws: Any, msg: dict[str, Any]) -> None:
        old = str(msg.get("name") or "").strip()
        new = str(msg.get("new_name") or "").strip()
        waveform_io = self._waveform_io()
        table = self._table()
        wave = waveform_io.find(table, old)
        if wave is None or wave.source == "official":
            await self._reply(ws, {"type": "import_error", "error": "官方波形不可重命名"})
            return
        if not new:
            await self._reply(ws, {"type": "import_error", "error": "新名字不能为空"})
            return
        old_path = self._wave_dir / f"{_slug(wave.name)}.json"
        try:
            old_path.write_text(
                json.dumps(
                    {"name": new, "version": wave.version, "frames": wave.frames},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            if _slug(new) != _slug(wave.name):
                with contextlib.suppress(OSError):
                    old_path.unlink()
        except OSError as exc:
            await self._reply(ws, {"type": "import_error", "error": f"重命名失败：{exc}"})
            return
        await self._reply(ws, {"type": "waveforms", "renamed": {"from": wave.name, "to": new},
                               "waveforms": self.list_waveforms()})

    # ──────────────────────────────────────────────────────────
    # FastAPI 应用（HTTP 静态页 + /ws）
    # ──────────────────────────────────────────────────────────
    def build_app(self) -> Any:
        node = self

        @asynccontextmanager
        async def lifespan(_app: Any) -> Any:
            await node.bind_to_loop()
            try:
                yield
            finally:
                await node.unbind()

        app = FastAPI(title="CoyoteLink 控制面板", docs_url=None, redoc_url=None, lifespan=lifespan)

        @app.get("/healthz")
        async def healthz() -> Any:  # noqa: ANN202
            return {
                "ok": True,
                "room": node.room,
                "clients": len(node._hub.clients),
                "waveforms": len(node.list_waveforms()),
            }

        @app.websocket("/ws")
        async def ws_endpoint(ws: WebSocket) -> None:  # noqa: ANN202
            await node._hub.register(ws)
            # 连上就先要一次设备快照 + 波形列表
            node.publish_select("", None, action="list")
            await node._reply(ws, {"type": "waveforms", "waveforms": node.list_waveforms()})
            await node._reply(ws, {"type": "hello", "room": node.room, "ts": _now_ms(), "host": socket.gethostname()})
            try:
                while True:
                    raw = await ws.receive_text()
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    await node.handle_client(ws, msg)
            except WebSocketDisconnect:
                pass
            except Exception as exc:  # noqa: BLE001
                node.log.warning("WS 异常：%s", exc)
            finally:
                node._hub.unregister(ws)

        index = node.web_root / "index.html"

        @app.get("/")
        async def root() -> Any:  # noqa: ANN202
            if not index.is_file():
                return JSONResponse({"error": f"缺少页面：{index}"}, status_code=500)
            return FileResponse(str(index))

        if node.web_root.is_dir():
            app.mount("/", StaticFiles(directory=str(node.web_root), html=True), name="web")

        return app

    # ──────────────────────────────────────────────────────────
    # 进程入口（★ 必须 uvicorn.run，见文件头）
    # ──────────────────────────────────────────────────────────
    @classmethod
    def main(cls, argv: list[str] | None = None) -> int:
        force_utf8()
        parser = argparse.ArgumentParser(
            prog="python -m agent.nodes.web",
            description="CoyoteLink 控制面板后端（T5）",
        )
        parser.add_argument("--host", default=_DEFAULT_HOST)
        parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
        parser.add_argument("--config", default=None, help="agent/config.toml 的路径")
        parser.add_argument("--room", default=None, help="覆盖 room")
        parser.add_argument("--log-level", default=None, help="覆盖日志级别")
        args = parser.parse_args(argv)

        config = Config.load(args.config)
        if args.room:
            config.room = args.room
        if args.log_level:
            config.log_level = args.log_level

        node = cls(config, host=args.host, port=args.port)
        app = node.build_app()

        node.log.info("控制面板：http://%s:%d（room=%s）", args.host, args.port, node.room)
        node.log.info("配套节点：python -m agent.core.cli run device / run camera / run pose / run audio")

        import uvicorn

        try:
            # ★ 必须用 uvicorn.run（不要 Server(..).serve()）—— 见文件头说明
            uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
        except KeyboardInterrupt:
            pass
        finally:
            with contextlib.suppress(Exception):
                node.log.info("控制面板退出（exit=%d）", 0 if node.error is None else 1)
        return 0 if node.error is None else 1


NODE = WebNode


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(WebNode.main())
