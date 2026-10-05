"""``CoyoteClient`` —— 对官方 SDK ``DglabSocket`` 的**薄封装**。

职责（任务书 T4 §6「对外接口」的实现基础）：

1. **连接 / 重连 / 状态机** —— 直连模式（``url``）与手动传输模式（``transport``）；
2. **设备发现** —— 维护 ``client_id → [Device]``，接入 App 时自动 ``request_devices``；
3. **底层操作透传** —— ``send_pulse`` / ``clear_*`` / ``reset_intensity`` 的 async 包装。

它**不**实现"开火"语义（波形选择、时长适配、叠加/抢占）—— 那是 :mod:`fire` 的活。

设计要点
--------

- **不 import 具体 SDK 内部**：只在默认 ``socket_factory`` 里延迟 ``from dglab_kit_python import DglabSocket``，
  单测可注入 ``FakeSocket``，无需联网；
- **事件回调可在任意线程触发**（SDK 的 ``handle_message`` 由我们的事件循环线程驱动，
  但用户也可能从别处 emit）—— 用户的 async 回调统一用 ``_schedule`` 桥接回事件循环；
- **手动传输模式**：SDK 不会自己连 WebSocket，``handle_open/close/message`` 必须由我们喂。
  入站由 :class:`~agent.nodes.coyote_v4.transport_adapter.RawV4Transport` 负责，
  本类只负责在构造时把该 transport 绑到 socket（``transport.bind``）。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .intensity import ChannelState, IntensityBook
from .transport_adapter import RawV4Transport

__all__ = [
    "ClientError",
    "Device",
    "ChannelState",
    "IntensityBook",
    "CoyoteClient",
    "channel_int",
    "STATE_IDLE",
    "STATE_CONNECTING",
    "STATE_WAITING_FOR_PEER",
    "STATE_PAIRED",
    "STATE_DISCONNECTED",
]

logger = logging.getLogger("coyotelink.coyote_v4.client")

#: SDK 状态机的字符串值（`DglabSocketState` 是 str-Enum，其 `.value` 就是这些）
STATE_IDLE = "idle"
STATE_CONNECTING = "connecting"
STATE_WAITING_FOR_PEER = "waiting_for_peer"
STATE_PAIRED = "paired"
STATE_DISCONNECTED = "disconnected"

#: 通道名 → 协议值。V4 用 0/1（SDK 的 `V4Channel.A/.B`）。
_CHANNEL_BY_NAME = {"A": 0, "B": 1}
_CHANNEL_BY_DIGIT = {"0": 0, "1": 1}


class ClientError(RuntimeError):
    """连接 / 参数 / 状态层面的错误（拿不到 targetId、没有可用设备……）。"""


def channel_int(channel: Any) -> int:
    """把通道规整成 ``0`` / ``1``。

    接受 ``"A"`` / ``"B"``（大小写不敏感）、``0`` / ``1``、``"0"`` / ``"1"``，
    以及 SDK 的 ``V4Channel.A``（``IntEnum``，值即 0/1）。其余一律 :class:`ClientError`。
    """
    if isinstance(channel, bool):  # bool 是 int 的子类，得先拦
        raise ClientError(f"通道不能是布尔值：{channel!r}")
    if isinstance(channel, int):
        if channel in (0, 1):
            return int(channel)
        raise ClientError(f"通道必须是 0 或 1，得到 {channel!r}")
    if isinstance(channel, str):
        key = channel.strip().upper()
        if key in _CHANNEL_BY_NAME:
            return _CHANNEL_BY_NAME[key]
        if key in _CHANNEL_BY_DIGIT:
            return _CHANNEL_BY_DIGIT[key]
    value = getattr(channel, "value", None)  # V4Channel 之类的枚举
    if isinstance(value, int) and not isinstance(value, bool) and value in (0, 1):
        return int(value)
    raise ClientError(f"无法识别的通道 {channel!r}（用 'A'/'B' 或 0/1）")


@dataclass(frozen=True)
class Device:
    """一个 App 暴露的设备（对应 SDK 的 ``slotId`` / ``name`` / ``type``）。"""

    slot_id: str
    name: str = ""
    type: str = ""
    raw: dict = field(default_factory=dict, compare=False, hash=False, repr=False)

    @classmethod
    def from_sdk(cls, data: Any) -> "Device":
        if not isinstance(data, dict):
            raise ClientError(f"设备描述必须是对象，得到 {type(data).__name__}")
        slot_id = data.get("slotId")
        if not isinstance(slot_id, str) or not slot_id:
            raise ClientError(f"设备缺少 slotId：{data!r}")
        return cls(
            slot_id=slot_id,
            name=str(data.get("name") or ""),
            type=str(data.get("type") or ""),
            raw=dict(data),
        )


class CoyoteClient:
    """封装一个 V4 ``DglabSocket``。一个节点进程建议只建一个实例。"""

    def __init__(
        self,
        *,
        url: str | None = None,
        transport: RawV4Transport | None = None,
        version: str | None = "v4",
        connect_timeout_ms: int = 8_000,
        response_timeout_ms: int = 8_000,
        socket_factory: Callable[..., Any] | None = None,
        on_action: Callable[[Any], Any] | None = None,
        on_devices: Callable[..., Any] | None = None,
        on_state: Callable[..., Any] | None = None,
        on_error: Callable[[Any], Any] | None = None,
        on_client_attached: Callable[[str], Any] | None = None,
        on_client_disconnected: Callable[[str], Any] | None = None,
        reconnect: bool = True,
        reconnect_initial: float = 0.5,
        reconnect_max: float = 15.0,
        reconnect_factor: float = 2.0,
        auto_request_devices: bool = True,
    ) -> None:
        self._url = url
        self._transport = transport
        self._socket_factory = socket_factory
        self._on_action = on_action
        self._on_devices = on_devices
        self._on_state_cb = on_state
        self._on_error_cb = on_error
        self._on_client_attached_cb = on_client_attached
        self._on_client_disconnected_cb = on_client_disconnected
        self._reconnect = reconnect
        self._reconnect_initial = reconnect_initial
        self._reconnect_max = reconnect_max
        self._reconnect_factor = reconnect_factor
        self._auto_request_devices = auto_request_devices

        self.log = logging.getLogger("coyotelink.coyote_v4.client")

        self._state: Any = STATE_IDLE
        self._target_id: str | None = None
        self._devices: dict[str, list[Device]] = {}
        #: 强度缓存（§5.9.2）：由 App 的 devices/slots.patch 事件驱动，开火时只读
        self._intensity = IntensityBook()
        self._order: list[str] = []  # 接入顺序，最后一个 = 最近接入
        self._peer_event = asyncio.Event()
        self._stopped = False
        self._reconnecting = False

        options: dict[str, Any] = {
            "connect_timeout": connect_timeout_ms,
            "response_timeout": response_timeout_ms,
        }
        if version is not None:
            options["version"] = version
        if url is not None:
            options["url"] = url
        self._socket = self._make_socket(options)
        self._register_listeners()
        if transport is not None:
            transport.bind(self._socket)

    # ──────────────────────────────────────────────────────────
    # 构造
    # ──────────────────────────────────────────────────────────
    def _make_socket(self, options: dict[str, Any]) -> Any:
        factory = self._socket_factory
        if factory is None:
            from dglab_kit_python import DglabSocket  # 延迟导入：单测可注入假 socket

            factory = DglabSocket
        return factory(**options)

    def _register_listeners(self) -> None:
        for event, handler in (
            ("state", self._handle_state),
            ("devices", self._handle_devices),
            ("client-attached", self._handle_client_attached),
            ("client-disconnected", self._handle_client_disconnected),
            ("action", self._handle_action),
            ("error", self._handle_error),
            ("close", self._handle_close),
        ):
            self._socket.on(event, handler)

    # ──────────────────────────────────────────────────────────
    # 只读属性
    # ──────────────────────────────────────────────────────────
    @property
    def socket(self) -> Any:
        """底层 SDK socket（逃生舱；测试里是 FakeSocket）。"""
        return self._socket

    @property
    def target_id(self) -> str | None:
        return self._target_id

    #: 兼容 SDK 的别名
    targetId = target_id

    @property
    def state(self) -> Any:
        return self._state

    @property
    def state_name(self) -> str:
        """状态的可比较字符串（SDK 的 str-Enum 取 ``.value``）。"""
        state = self._state
        value = getattr(state, "value", state)
        return str(value) if value is not None else STATE_IDLE

    @property
    def client_ids(self) -> list[str]:
        """已接入的 App（最近接入的排最后）。优先用本地记录，空则回退到 SDK 缓存。"""
        if self._order:
            return list(self._order)
        return self._socket_client_ids()

    @property
    def is_connected(self) -> bool:
        return self.state_name in (STATE_WAITING_FOR_PEER, STATE_PAIRED)

    @property
    def is_paired(self) -> bool:
        return bool(self.client_ids)

    @property
    def default_client_id(self) -> str | None:
        """最近接入的 App；没有则 None。"""
        ids = self.client_ids
        return ids[-1] if ids else None

    def devices_for(self, client_id: str) -> list[Device]:
        """某 App 的设备列表（优先本地缓存，空则回退 SDK 缓存）。"""
        cached = self._devices.get(client_id)
        if cached:
            return list(cached)
        return self._devices_from_socket(client_id)

    def default_slot_id(self, client_id: str | None = None) -> str | None:
        """某 App 的第一个设备 ``slot_id``（默认取最近接入的 App）。"""
        cid = client_id or self.default_client_id
        if cid is None:
            return None
        devices = self.devices_for(cid)
        return devices[0].slot_id if devices else None

    # ── 强度缓存（§5.9.2：以 App 事件回报为准，不轮询）────────────
    @property
    def intensity(self) -> IntensityBook:
        """强度缓存（只读查询：真实强度 / 上限）。由 ``devices`` 事件驱动更新。"""
        return self._intensity

    def intensity_strength(
        self, client_id: str | None = None, slot_id: str | None = None, channel: Any = "A"
    ) -> int | None:
        """最近已知**真实强度**（``None`` = 还没收到过 App 回报）。"""
        cid, sid = self._resolve_target(client_id, slot_id)
        if cid is None or sid is None:
            return None
        return self._intensity.strength(cid, sid, channel)

    def intensity_limit(
        self, client_id: str | None = None, slot_id: str | None = None, channel: Any = "A"
    ) -> int | None:
        """最近已知**上限**（舒适 > 绝对 > intensityMax；都未知为 ``None``）。"""
        cid, sid = self._resolve_target(client_id, slot_id)
        if cid is None or sid is None:
            return None
        return self._intensity.limit(cid, sid, channel)

    def _resolve_target(
        self, client_id: str | None, slot_id: str | None
    ) -> tuple[str | None, str | None]:
        cid = client_id or self.default_client_id
        if cid is None:
            return None, None
        sid = slot_id or self.default_slot_id(cid)
        return cid, sid

    # ──────────────────────────────────────────────────────────
    # 连接生命周期
    # ──────────────────────────────────────────────────────────
    async def start(self) -> str:
        """连接（直连模式）或开始等握手（手动模式），返回 ``targetId``。"""
        self._stopped = False
        result = await self._socket.connect()
        target = (result or {}).get("targetId") if isinstance(result, dict) else None
        if not isinstance(target, str) or not target:
            raise ClientError(f"connect() 未返回 targetId：{result!r}")
        self._target_id = target
        self.log.info("已连接，targetId=%s（state=%s）", target, self.state_name)
        return target

    async def wait_for_peer(self, timeout: float = 120.0) -> str:
        """等待 App 接入，返回其 ``client_id``；超时 :class:`ClientError`。"""
        if self._order:
            return self._order[-1]
        self._peer_event.clear()
        if self._order:  # 竞态兜底：clear 与 wait 之间可能刚接入
            return self._order[-1]
        try:
            await asyncio.wait_for(self._peer_event.wait(), timeout)
        except asyncio.TimeoutError as exc:
            raise ClientError(f"等待 App 接入超时（{timeout:.0f}s）") from exc
        return self._order[-1]

    async def refresh_devices(self, client_id: str) -> Any:
        """主动 ``request_devices`` 并刷新本地缓存（失败只 warning，不抛出）。"""
        try:
            result = await self.request_devices(client_id)
        except Exception as exc:  # noqa: BLE001 - 设备查询失败不该打死节点
            self.log.warning("request_devices(%s) 失败：%s", client_id, exc)
            return None
        devices = result.get("devices") if isinstance(result, dict) else None
        if isinstance(devices, list):
            self._devices[client_id] = self._parse_devices(devices)
        return result

    async def close(self) -> None:
        """停止重连并断开（幂等）。"""
        self._stopped = True
        try:
            await self._socket.disconnect()
        except Exception as exc:  # noqa: BLE001
            self.log.debug("disconnect() 失败（忽略）：%s", exc)

    # ──────────────────────────────────────────────────────────
    # 底层 V4 操作（async 包装 —— fire 层调用这些）
    # ──────────────────────────────────────────────────────────
    async def request_devices(self, client_id: str) -> Any:
        return await self._socket.request_devices(client_id)

    async def send_pulse(
        self,
        client_id: str,
        slot_id: str,
        channel: Any,
        duration_ms: int,
        frames: list[str],
        options: dict[str, Any] | None = None,
    ) -> Any:
        """下发波形（= 真正"开火"）。``options`` 见 README「V4 操作选项」。"""
        return await self._socket.send_pulse(
            client_id, slot_id, channel_int(channel), duration_ms, frames, options
        )

    async def clear_pulse(self, client_id: str, slot_id: str, channel: Any) -> Any:
        return await self._socket.clear_pulse(client_id, slot_id, channel_int(channel))

    async def clear_operate(
        self, client_id: str, options: dict[str, Any] | None = None
    ) -> Any:
        """清理全部 / 指定设备 / 指定通道的任务。"""
        return await self._socket.clear_operate(client_id, options)

    async def reset_intensity(
        self, client_id: str, slot_id: str, channel: Any, options: dict[str, Any] | None = None
    ) -> Any:
        return await self._socket.reset_intensity(
            client_id, slot_id, channel_int(channel), options
        )

    # ── 强度操作（“一键开火”的核心：光发波形不调强度，基本没感觉）────
    async def add_intensity(
        self, client_id: str, slot_id: str, channel: Any, value: int,
        options: dict[str, Any] | None = None,
    ) -> Any:
        """**相对增加**通道强度（V4 动作码 AddIntensity=3）。"""
        return await self._socket.add_intensity(
            client_id, slot_id, channel_int(channel), value, options
        )

    async def reduce_intensity(
        self, client_id: str, slot_id: str, channel: Any, value: int,
        options: dict[str, Any] | None = None,
    ) -> Any:
        """**相对减少**通道强度（SDK 里叫 reduce_strength）。"""
        return await self._socket.reduce_strength(
            client_id, slot_id, channel_int(channel), value, options
        )

    async def set_temp_intensity(
        self, client_id: str, slot_id: str, channel: Any, value: int,
        duration_ms: int, options: dict[str, Any] | None = None,
    ) -> Any:
        """临时强度（V4 动作码 SetTempIntensity=4）：``duration_ms`` 后自动回落。"""
        return await self._socket.set_temp_intensity(
            client_id, slot_id, channel_int(channel), value, duration_ms, options
        )

    async def set_intensity(
        self, client_id: str, slot_id: str, channel: Any, value: int,
        options: dict[str, Any] | None = None,
    ) -> Any:
        """**绝对设定**强度 = reset + add（SDK 没提供直接的绝对设定）。"""
        ch = channel_int(channel)
        await self._socket.reset_intensity(client_id, slot_id, ch, options)
        if value > 0:
            return await self._socket.add_intensity(client_id, slot_id, ch, value, options)
        return None

    async def stop(
        self,
        channel: Any | None = None,
        *,
        client_id: str | None = None,
        slot_id: str | None = None,
    ) -> Any:
        """急停：``channel=None`` → 清理该 App 全部；否则只清该通道。"""
        cid = client_id or self.default_client_id
        if cid is None:
            raise ClientError("没有已接入的 App，无法急停")
        if channel is None:
            self.log.info("急停：清理 client=%s 的全部任务", cid)
            return await self.clear_operate(cid)
        sid = slot_id or self.default_slot_id(cid)
        if sid is None:
            raise ClientError(f"client={cid} 没有可用设备，无法按通道急停")
        ch = channel_int(channel)
        self.log.info("急停：清理 client=%s slot=%s channel=%s", cid, sid, ch)
        return await self.clear_pulse(cid, sid, ch)

    # ──────────────────────────────────────────────────────────
    # 事件桥接
    # ──────────────────────────────────────────────────────────
    def _handle_state(self, state: Any, previous: Any = None) -> None:
        self._state = state
        self.log.debug("状态 %s → %s", previous, self.state_name)
        self._dispatch(self._on_state_cb, state, previous)

    def _handle_devices(self, devices: Any, client_id: str | None = None) -> None:
        cid = client_id or self.default_client_id
        if not isinstance(cid, str) or not cid:
            return
        parsed = self._parse_devices(devices)
        self._devices[cid] = parsed
        # ★ 强度缓存由 App 事件驱动（devices / devices.patch / slots.patch 都会走到这里）。
        #   开火时只读缓存、不再 query，避免延迟翻倍（§5.9.4）。
        self._intensity.apply_devices(
            cid, list(devices) if isinstance(devices, (list, tuple)) else []
        )
        self.log.info("设备更新 client=%s：%s", cid, [d.slot_id for d in parsed])
        self._dispatch(self._on_devices, parsed, cid)

    def _handle_client_attached(self, client_id: str) -> None:
        if client_id not in self._order:
            self._order.append(client_id)
        self._peer_event.set()
        self.log.info("App 接入 client=%s（当前 %d 个）", client_id, len(self._order))
        self._dispatch(self._on_client_attached_cb, client_id)
        if self._auto_request_devices:
            self._schedule(self.refresh_devices(client_id))

    def _handle_client_disconnected(self, client_id: str) -> None:
        self._order = [c for c in self._order if c != client_id]
        self._devices.pop(client_id, None)
        self._intensity.forget(client_id)
        self.log.info("App 断开 client=%s（剩余 %d 个）", client_id, len(self._order))
        self._dispatch(self._on_client_disconnected_cb, client_id)

    def _handle_action(self, action: Any) -> None:
        self.log.debug("收到 App 动作反馈：%r", action)
        self._dispatch(self._on_action, action)

    def _handle_error(self, error: Any) -> None:
        self.log.warning("SDK 错误：%s", error)
        self._dispatch(self._on_error_cb, error)

    def _handle_close(self, event: Any = None) -> None:
        self.log.warning("连接关闭：%r", event)
        self._state = STATE_DISCONNECTED
        self._dispatch(self._on_state_cb, STATE_DISCONNECTED, None)
        if self._reconnect and not self._stopped and not self._reconnecting:
            self._schedule(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """指数退避重连（D9）。手动模式下由 transport 端重新建立链路并喂 hello。"""
        if self._reconnecting:
            return
        self._reconnecting = True
        delay = self._reconnect_initial
        try:
            while not self._stopped:
                await asyncio.sleep(delay)
                if self._stopped:
                    return
                try:
                    await self.start()
                except Exception as exc:  # noqa: BLE001 - 重连失败继续退避
                    self.log.warning("重连失败（%.1fs 后再试）：%s", delay, exc)
                    delay = min(delay * self._reconnect_factor, self._reconnect_max)
                else:
                    self.log.info("重连成功")
                    return
        finally:
            self._reconnecting = False

    # ──────────────────────────────────────────────────────────
    # 内部工具
    # ──────────────────────────────────────────────────────────
    @staticmethod
    def _parse_devices(devices: Any) -> list[Device]:
        out: list[Device] = []
        for entry in devices or []:
            try:
                out.append(Device.from_sdk(entry))
            except ClientError as exc:
                logger.warning("忽略无法解析的设备：%s", exc)
        return out

    def _socket_client_ids(self) -> list[str]:
        ids = getattr(self._socket, "client_ids", None)
        if ids is None:
            ids = getattr(self._socket, "clientIds", None)
        return list(ids) if ids else []

    def _devices_from_socket(self, client_id: str) -> list[Device]:
        getter = getattr(self._socket, "get_client", None)
        if getter is None:
            return []
        client = getter(client_id)
        raw = getattr(client, "devices", None) if client is not None else None
        return self._parse_devices(raw)

    def _dispatch(self, callback: Callable[..., Any] | None, *args: Any) -> None:
        if callback is None:
            return
        try:
            result = callback(*args)
        except Exception as exc:  # noqa: BLE001 - 用户回调抛错不能拖垮节点
            self.log.error("回调 %r 抛异常：%s", callback, exc)
            return
        if inspect.isawaitable(result):
            self._schedule(result)

    def _schedule(self, awaitable: Any) -> None:
        """把 awaitable 挂到事件循环（同线程则 create_task，否则另起循环兜底）。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and not loop.is_closed():
            loop.create_task(awaitable)
        else:  # 例如在同步单测里直接调用事件处理
            asyncio.run(awaitable)

    def __repr__(self) -> str:
        return (
            f"<CoyoteClient state={self.state_name} target={self._target_id!r} "
            f"clients={len(self.client_ids)} url={self._url!r}>"
        )
