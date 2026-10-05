"""郊狼 V4 控制端节点（T4）—— 对外导出 :class:`CoyoteV4Node`。

职责
----

- **订阅** ``command/fire``（开火）、``command/clear``（急停）、``command/raw_v4``（手动传输入站）
- **发布** ``event/fire``（开火审计）
- 对外形态：``class CoyoteV4Node(Node)``

三层结构
--------

========================  ====================================================
模块                       职责
========================  ====================================================
``client.py``            封装官方 ``DglabSocket``：连接 / 重连 / 状态机 / 设备发现
``fire.py``              「开火」高层语义：波形 + 通道 + 时长 + 优先级 + 叠加
``waveform_io.py``       V2 / V3 波形导入、识别、校验、时长适配、合并查询
``transport_adapter.py`` 手动传输模式：``command/raw_v4`` ⇄ SDK
========================  ====================================================

传输模式
--------

- ``url=None``（默认）→ **手动传输模式**：SDK 不自己连 WebSocket，
  入站数据来自订阅 ``command/raw_v4``，出站数据也发到 ``command/raw_v4``（回程）。
  ⚠️ 出站与入站共用同一个 key，本节点会用**内容规范化去重**忽略自己发出的回环
  （Zenoh 本地 session 会收到自己 publish 的消息）。
- ``url=<ws://…>`` → **直连模式**：SDK 自己连服务端（真机 / 自建 V4 服务端）。

启动时序
--------

``setup()`` **不阻塞**等待连接（否则手动模式下永远等不到 App 接入就卡死 setup）。
连接在后台任务里做，失败按退避重试；掉线后由 :class:`CoyoteClient` 自动重连。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import deque
from pathlib import Path
from typing import Any, Mapping

from agent.core import Node, topics
from agent.core.messages import EventFire

from . import fire as fire_mod
from . import waveform_io
from .client import ClientError, CoyoteClient, Device, channel_int
from .fire import FireError, FireRequest, FireResult, build_options, fire
from .intensity import ChannelState, IntensityBook
from .transport_adapter import PayloadError, RawV4Transport, payload_to_text
from .waveform_io import Waveform, WaveformError

__all__ = [
    "CoyoteV4Node",
    "CoyoteClient",
    "ClientError",
    "Device",
    "ChannelState",
    "IntensityBook",
    "channel_int",
    "FireRequest",
    "FireResult",
    "FireError",
    "build_options",
    "fire",
    "fire_mod",
    "waveform_io",
    "Waveform",
    "WaveformError",
    "RawV4Transport",
    "payload_to_text",
    "PayloadError",
]

#: 出站回环去重窗口（最近多少条）
_ECHO_WINDOW = 32

#: ``event/link`` 的重播间隔（秒）—— 面板晚开/首帧丢包都能自愈，见 ``publish_link``
_LINK_REPEAT_S = 2.0


def _normalize_frame(text: str) -> str:
    """把一帧 V4 文本规范化成可比较的形态（JSON 则解析后按键排序重排）。"""
    stripped = text.strip()
    try:
        return json.dumps(
            json.loads(stripped), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
    except (ValueError, TypeError):
        return stripped


def _url_from_config(config: Any) -> str | None:
    """从 ``agent/config.toml`` 的 ``[coyote_v4] url`` 读**对端 relay** 地址。

    ⚠️ 取不到返回 ``None`` = 手动传输模式 —— 那条路需要有人扮演 V4 服务器
    （先发 hello 帧，SDK 才拿得到 ``targetId``），单机跑没有 ⇒ 会一直 ``connect_timeout``。
    正式部署请显式配 url（模板见 ``agent/config.example.toml``）。
    """
    raw = getattr(config, "raw", None) or {}
    section = raw.get("coyote_v4") or {}
    if isinstance(section, dict):
        value = section.get("url")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class CoyoteV4Node(Node):
    """郊狼 V4 控制端节点。

    :param config: 节点配置（默认 :meth:`agent.core.Config.load`）
    :param url: 要连的 V4 服务地址（**对端 relay**，如 ``ws://192.168.50.251:9997``）。
        传 ``None`` → 读 ``agent/config.toml`` 的 ``[coyote_v4] url``；
        再没有就是**手动传输模式**（⚠️ 单机会一直 connect_timeout，见 ``_url_from_config``）。
    :param socket_factory: 覆盖 SDK 构造（单测注入 ``FakeSocket``）；``None`` → 用 ``DglabSocket``
    :param transport: 注入的 :class:`RawV4Transport`；``None`` 且手动模式时自动新建
    :param waveforms: 注入的波形查找表（``name → Waveform``）；给了就跳过磁盘加载
    :param waveform_dir: 导入波形目录（默认 ``<data_dir>/waveforms``）
    :param auto_connect: 是否在 ``setup()`` 后台自动连接（默认 True）
    :param client_kwargs: 透传给 :class:`CoyoteClient` 的其它参数（如 ``reconnect=False``）
    """

    name = "coyote_v4"

    def __init__(
        self,
        config: Any | None = None,
        *,
        url: str | None = None,
        socket_factory: Any | None = None,
        transport: RawV4Transport | None = None,
        waveforms: Mapping[str, Waveform] | None = None,
        waveform_dir: str | Path | None = None,
        auto_connect: bool = True,
        connect_retry_initial: float = 0.5,
        connect_retry_max: float = 15.0,
        client_kwargs: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(config)
        #: 显式传的 url 优先 → 否则读配置 → 都没有才是手动传输模式
        self._url = url if url is not None else _url_from_config(self.config)
        self._socket_factory = socket_factory
        self._transport = transport
        self._waveforms = waveforms
        self._waveform_dir = (
            Path(waveform_dir) if waveform_dir is not None else self.config.data_dir / "waveforms"
        )
        self._auto_connect = auto_connect
        self._connect_retry_initial = connect_retry_initial
        self._connect_retry_max = connect_retry_max
        self._client_kwargs = dict(client_kwargs or {})

        self._table: dict[str, Waveform] = {}
        self._client: CoyoteClient | None = None
        self._pub_fire: Any = None
        self._pub_link: Any = None      # event/link（面板上的 Relay 指示灯）
        self._pub_raw: Any = None
        self._sub_fire: Any = None
        self._sub_clear: Any = None
        self._sub_raw: Any = None
        self._sub_pulse: Any = None
        self._connect_task: asyncio.Task[Any] | None = None
        self._link_task: asyncio.Task[Any] | None = None
        #: 上一次发给面板的链路状态（不含 ts）—— 周期重播用，见 publish_link
        self._link_last: dict[str, Any] | None = None
        #: 手机 App（被控端）在不在 + 是哪一台 —— 面板"手机 DG-LAB App"那行读它
        self._app_attached = False
        self._app_client_id = ""
        self._app_slots: list[str] = []
        #: 最近一次 App 回报的强度/上限（仪表盘用；None = 还没收到过）
        self._strength: dict[str, Any] = {}
        #: 这一次开火的"谁发的 / 打给谁"（跟 event/fire 一起发出去，面板被控端视图读它）
        self._fire_meta: dict[str, str] = {}
        self._recent_out: deque[str] = deque(maxlen=_ECHO_WINDOW)
        self._manual = self._url is None

    # ──────────────────────────────────────────────────────────
    # 生命周期
    # ──────────────────────────────────────────────────────────
    async def setup(self) -> None:
        self._table = self._load_waveforms()
        self._pub_fire = self.publish(topics.EVENT_FIRE)
        self._pub_link = self.publish(topics.EVENT_LINK)      # ★ 面板上的 Relay 指示灯

        if self._manual:
            if self._transport is None:
                self._transport = RawV4Transport(self._send_raw_v4, name=self.name)
            self._pub_raw = self.publish(topics.COMMAND_RAW_V4)

        self._client = CoyoteClient(
            url=self._url,
            transport=self._transport,
            socket_factory=self._socket_factory,
            on_action=self._on_action,
            on_devices=self._on_devices,
            # ★ 手机 App 接入/断开要**立刻**告诉面板（`event/link`）。
            #   以前只有 App 每 ~20 秒报一次设备时才会顺带刷新，
            #   断开更是完全没人说 —— 面板上"手机 DG-LAB App"那行永远停在"已连接"（实测反馈）。
            on_client_attached=self._on_app_attached,
            on_client_disconnected=self._on_app_disconnected,
            **self._client_kwargs,
        )

        self._sub_fire = self.subscribe(topics.COMMAND_FIRE, self.on_fire)
        self._sub_clear = self.subscribe(topics.COMMAND_CLEAR, self.on_clear)
        # ★ 把订阅的真 key 说出来：面板点「开火」没反应时，第一件事就是核对
        #   "两边是不是订到同一个 key 上"（实测踩过 room 不一致导致静默收不到）。
        self.log.info("已订阅：%s（开火）/ %s（急停）", self._sub_fire.key, self._sub_clear.key)
        # 模拟主机截获的 App 原始脉冲流（T9）：两种传输模式下都要接
        self._sub_pulse = self.subscribe(topics.COMMAND_PULSE_RAW, self.on_pulse_raw)
        if self._manual:
            self._sub_raw = self.subscribe(topics.COMMAND_RAW_V4, self.on_raw_v4)

        if self._auto_connect:
            self._connect_task = asyncio.create_task(self._connect_forever())
        self.publish_link(
            "manual" if self._manual else "connecting",
            detail="手动传输模式" if self._manual else f"正在连 {self._url}…",
        )
        # ★ 周期重播链路状态（见 publish_link 的说明）：面板晚开也能看到，不怕首帧丢
        self._link_task = asyncio.create_task(self._link_loop())

        self.log.info(
            "coyote_v4 就绪：%d 条波形，模式=%s",
            len(self._table),
            "手动传输" if self._manual else f"直连 {self._url}",
        )

    async def teardown(self) -> None:
        for attr in ("_link_task", "_connect_task"):
            task = getattr(self, attr, None)
            setattr(self, attr, None)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        client = self._client
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()
        self.log.info("coyote_v4 已清理")

    # ──────────────────────────────────────────────────────────
    # 订阅 handler
    # ──────────────────────────────────────────────────────────
    async def on_fire(self, msg: Any) -> None:
        """``command/fire`` → 一次开火 + 一条 ``event/fire`` 审计。

        ★ ``target``（可选）：打**哪台机器**上连着的郊狼。

        - 不给 / ``"local"`` / 本机主机名 → 只打本机（默认，也是面板"本机"那一项）；
        - ``"all"`` → 房间里谁都打（"两边一起"的旧行为）；
        - 别的主机名 → 跳过（不是给我的）。

        为什么要有它：房间 = 同一个 key 前缀，``command/fire`` 又是**跨机白名单**里的
        key —— 不给 target 的话，我的面板点一下开火，**房间里每台机器的郊狼都会被打**
        （实测就是"两边一起动"）。要做"控制对方 / 被对方控制"，必须能指名道姓。
        """
        payload: Any = None
        try:
            payload = self._as_mapping(msg)
            target = ""
            if isinstance(payload, Mapping):
                target = str(payload.get("target") or "").strip().lower()
            if target and target not in ("local", "all"):
                from agent.core.node import _HOSTNAME as host

                if target != host.lower():
                    self.log.debug("开火 %s 不是给我的（我是 %s），跳过", target, host)
                    return
            source = "rules"
            if isinstance(payload, Mapping) and isinstance(payload.get("source"), str):
                source = str(payload["source"])
            # 谁发的 / 打给谁：跟随这次开火一起进 event/fire（面板被控端视图要显示）
            self._fire_meta = {
                "by": str(payload.get("by") or "") if isinstance(payload, Mapping) else "",
                "target": str(payload.get("target") or "") if isinstance(payload, Mapping) else "",
            }
            request = FireRequest.from_command(payload)
            result = await fire_mod.fire(self._client, self._table, request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 开火失败不能打死节点
            self.log.error("开火失败（指令=%r）：%s", msg, exc)
            # ★ 失败也要回面板：否则面板只会说"后端已受理"，用户点了没反应还以为是坏的
            self._publish_fire_failure(exc, payload if isinstance(payload, Mapping) else {})
            return
        self._publish_fire_event(result, source)
        self.log.info(
            "已开火：%s v%d 通道%s %dms（%d 帧，%s）强度[前=%s→目标=%s 上限=%s%s]",
            result.waveform,
            result.version,
            result.channel,
            result.duration_ms,
            result.frame_count,
            "叠加" if result.overlay else "抢占",
            result.intensity_before,
            result.intensity_requested,
            result.intensity_limit,
            "，已限幅" if result.intensity_clamped else "",
        )

    async def on_clear(self, msg: Any) -> None:
        """``command/clear`` → 急停（``channel=None`` 清理该 App 全部任务）。"""
        try:
            if self._client is None:
                return
            payload = self._as_mapping(msg)
            data: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
            channel = data.get("channel")
            client_id = data.get("client_id") or data.get("clientId")
            slot_id = data.get("slot_id") or data.get("slotId")
            await self._client.stop(channel, client_id=client_id, slot_id=slot_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.log.error("急停失败（指令=%r）：%s", msg, exc)

    async def on_raw_v4(self, payload: Any) -> None:
        """``command/raw_v4`` → 喂给 SDK（跳过自己发出的回环）。"""
        try:
            text = payload_to_text(payload)
        except PayloadError as exc:
            self.log.warning("raw_v4 payload 无法还原成文本：%s", exc)
            return
        if self._is_local_echo(text):
            self.log.debug("忽略 raw_v4 本地回环：%s", text[:120])
            return
        transport = self._transport
        if transport is None:
            self.log.warning("未配置 transport，丢弃 raw_v4：%s", text[:120])
            return
        await transport.feed(text)

    async def on_pulse_raw(self, payload: Any) -> None:
        """``command/pulse_raw`` → 把模拟主机截获的 App 原始脉冲流下发到被控端（T9）。

        ``sim_coyote`` 每 100ms 发一条；这里翻译成 V4 的 ``device.op``：

        * ``pulses``    → ``send_pulse``（AppendPulseData，**追加不替换**）——
          所以它和 ``rules`` 的惩罚能**叠加**，互不抢占；
        * ``intensity`` → ``add_intensity``（相对，减少即负值）/ ``set_intensity``（绝对）。
        """
        data = self._as_mapping(payload)
        client = self._client
        if not isinstance(data, Mapping) or client is None or not client.is_paired:
            return
        client_id = client.default_client_id
        slot_id = client.default_slot_id(client_id) if client_id else None
        if not client_id or not slot_id:
            self.log.debug("pulse_raw：还没配对到设备，丢弃")
            return
        try:
            for pulse in data.get("pulses") or []:
                frames = list(pulse.get("frames") or [])
                if not frames:
                    continue
                await client.send_pulse(
                    client_id, slot_id, pulse.get("channel", "A"),
                    int(pulse.get("duration_ms") or 100), frames, {"version": 3},
                )
            for item in data.get("intensity") or []:
                value = int(item.get("value") or 0)
                if value == 0:
                    continue
                channel = item.get("channel", "A")
                if item.get("op") == "set":
                    await client.set_intensity(client_id, slot_id, channel, value)
                else:
                    await client.add_intensity(client_id, slot_id, channel, value)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单条脉冲失败不能打死节点
            self.log.warning("pulse_raw 下发失败：%s", exc)

    # ──────────────────────────────────────────────────────────
    # 内部
    # ──────────────────────────────────────────────────────────
    def _send_raw_v4(self, text: Any) -> None:
        """transport 的出站回调：把 SDK 要发的帧发到 ``command/raw_v4``（回程）。"""
        if not isinstance(text, str):
            text = payload_to_text(text)
        self._recent_out.append(_normalize_frame(text))
        publisher = self._pub_raw
        if publisher is None:
            self.log.warning("未声明 raw_v4 发布者，出站丢弃：%s", text[:120])
            return
        publisher.put(text)

    def _is_local_echo(self, text: str) -> bool:
        return _normalize_frame(text) in self._recent_out

    def _load_waveforms(self) -> dict[str, Waveform]:
        if self._waveforms is not None:
            return {k.strip().upper(): v for k, v in self._waveforms.items()}
        try:
            official = waveform_io.official_waveforms()
        except WaveformError as exc:
            self.log.warning("读不到官方波形（%s），只用导入波形", exc)
            official = {}
        return waveform_io.load_all(self._waveform_dir, official=official)

    def _publish_fire_event(self, result: FireResult, source: str) -> None:
        event = EventFire(
            ts=int(time.time() * 1000),
            source=source,
            waveform=result.waveform,
            channel=result.channel,
            duration_ms=result.duration_ms,
            # 审计要能回答“开了什么、多久、多少帧、叠没叠加、打到哪”
            version=result.version,
            frame_count=result.frame_count,
            priority=result.priority,
            overlay=result.overlay,
            client_id=result.client_id,
            slot_id=result.slot_id,
            ok=True,
            by=self._fire_meta.get("by") or None,
            target=self._fire_meta.get("target") or None,
        )
        self._pub_fire.put(event)

    def _publish_fire_failure(self, exc: Exception, payload: Mapping[str, Any]) -> None:
        """开火失败 → 也发一条 ``event/fire``（``ok=False``），让面板**当场显示原因**。

        为什么值得单独发一条：面板点「开火」时，web 节点只能回"我收到了"；
        真正的失败（"没有已接入的 App" / "没有可用设备"）发生在郊狼这一侧，
        以前只写进 `logs/coyote_v4.log` —— 用户看到"后端已受理"却什么都没发生。
        """

        def _int(key: str, default: int) -> int:
            value = payload.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else default

        self._pub_fire.put(EventFire(
            ts=int(time.time() * 1000),
            source=str(payload.get("source") or "web"),
            waveform=str(payload.get("waveform") or "?"),
            channel=str(payload.get("channel") or "A"),
            duration_ms=_int("duration_ms", 0),
            priority=_int("priority", 1),
            overlay=bool(payload.get("overlay", True)),
            ok=False,
            error=str(exc),
            by=self._fire_meta.get("by") or None,
            target=self._fire_meta.get("target") or None,
        ))

    @staticmethod
    def _as_mapping(msg: Any) -> Any:
        """订阅侧拿到的消息可能是 dict，也可能是（解码失败的）bytes。"""
        if isinstance(msg, (bytes, bytearray, memoryview)):
            from agent.core import messages as core_messages

            try:
                return core_messages.from_bytes(bytes(msg))
            except Exception:  # noqa: BLE001 - 坏消息已由基类 warning，这里降级
                return None
        return msg

    async def _connect_forever(self) -> None:
        """后台连接循环：失败退避重试；成功即返回（掉线由 client 自己重连）。

        每轮都把结果发到 ``event/link`` —— 面板上的「郊狼 Relay」指示灯就是它。
        （以前只有日志，用户得翻控制台才知道连没连上。）
        """
        client = self._client
        assert client is not None
        delay = self._connect_retry_initial
        attempt = 0
        while True:
            attempt += 1
            try:
                target = await client.start()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 连接失败重试，不打死节点
                self.log.warning("连接失败（%.1fs 后重试）：%s", delay, exc)
                self.publish_link(
                    "error",
                    detail=f"第 {attempt} 次尝试失败：{exc}",
                    hint="Relay 没起？新开一个窗口跑 python tools\\relay.py",
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._connect_retry_max)
                continue
            self.log.info("已连接，targetId=%s", target)
            self.publish_link("connected", detail="已拿到 targetId", target_id=target)
            return

    async def _link_loop(self) -> None:
        """周期重播链路状态 —— 见 :meth:`publish_link` 里"为什么要重播"。"""
        while True:
            await asyncio.sleep(_LINK_REPEAT_S)
            self.publish_link()

    def publish_link(
        self,
        state: str | None = None,
        *,
        detail: str = "",
        hint: str = "",
        target_id: Any = None,
    ) -> None:
        """发一条 ``event/link``（Relay 侧状态）。面板上的指示灯读它。

        ``state=None`` = **重播上一次**（由 :meth:`_link_loop` 周期调用）。

        为什么要重播：Zenoh 的发布/订阅要**先匹配**，节点刚起来那一瞬间发的
        "connecting/connected" 很可能在匹配完成前就扔出去了（直接丢，不排队），
        面板后开、或浏览器刷新之后也永远看不到状态。
        :mod:`agent.nodes.sim_coyote` 那边靠 2s 轮询天然躲过了这个坑，这里补上同款。
        """
        if self._manual:
            state = "manual"
            detail = detail or "手动传输模式（没配 [coyote_v4] url）"
        if state is not None:
            self._link_last = {
                "source": self.name,
                "kind": "relay",
                "state": state,                 # connected / connecting / error / manual
                "url": self._url or "",
                "detail": detail,
                "hint": hint,
                "target_id": target_id,
                # ★ 被控端（手机 App）状态：面板的"手机 DG-LAB App"那行 + 仪表盘读它。
                #   接入/断开/设备上报都会立刻 publish_link（见那几个回调）。
                "app_attached": bool(self._app_attached),
                "app_client_id": self._app_client_id,
                "slots": list(self._app_slots),
                "strength": dict(self._strength),
            }
        last = self._link_last
        pub = self._pub_link
        if last is None or pub is None:
            return
        last = {**last, "ts": int(time.time() * 1000)}
        self._link_last = last
        pub.put(dict(last))

    # ── 回调（client → 节点日志）──
    def _on_action(self, action: Any) -> None:
        self.log.info("收到 App 动作反馈：%r", action)

    def _on_app_attached(self, client_id: str) -> None:
        """手机 DG-LAB App 接入（被控端上线）→ 立刻刷面板那一行。"""
        self._app_client_id = str(client_id)
        self._app_attached = True
        self.log.info("手机 App 已接入：clientId=%s", self._app_client_id)
        self.publish_link()

    def _on_app_disconnected(self, client_id: str) -> None:
        """手机 App 断开 → 立刻刷面板（以前断开是**没人说**的，UI 一直显示已连接）。"""
        if str(client_id) == self._app_client_id:
            self._app_client_id = ""
        self._app_attached = False
        self._app_slots = []
        self.log.warning("手机 App 已断开：clientId=%s（面板会显示未连接）", client_id)
        self.publish_link()

    def _on_devices(self, devices: list[Device], client_id: str) -> None:
        self.log.info("设备列表更新 client=%s：%s", client_id, [d.slot_id for d in devices])
        self._app_client_id = str(client_id)
        self._app_attached = True
        self._app_slots = [str(getattr(d, "slot_id", "")) for d in devices]
        book = self._client.intensity if self._client is not None else None
        if book is not None and devices:
            slot = devices[0].slot_id
            a = book.get(client_id, slot, "A")
            self.log.info(
                "强度缓存 client=%s slot=%s：A 真实=%s 上限=%s",
                client_id, slot, a.strength, a.limit,
            )
            self._strength = {
                "client_id": str(client_id),
                "slot_id": str(slot),
                "A": {"real": a.strength, "limit": a.limit},
                "B": {
                    "real": book.get(client_id, slot, "B").strength,
                    "limit": book.get(client_id, slot, "B").limit,
                },
            }
        self.publish_link()      # ★ 顺手把"App 在不在 / 强度多少"推给面板（见 publish_link）

    # ── 便利属性 ──
    @property
    def client(self) -> CoyoteClient | None:
        return self._client

    @property
    def waveforms(self) -> dict[str, Waveform]:
        return dict(self._table)

    @property
    def is_paired(self) -> bool:
        return self._client is not None and self._client.is_paired


#: 供 `agent.core.cli` 明确指定本模块的节点类（避免同名歧义）
NODE = CoyoteV4Node


if __name__ == "__main__":  # pragma: no cover - 直接 import 的运行方式
    raise SystemExit(CoyoteV4Node.main())
