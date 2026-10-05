"""强度缓存与限幅 —— 「以 App 回报为准」的运行时状态（T4 §6.2 / 架构 §5.9）。

为什么单独一个模块
------------------

郊狼的输出 = **强度 × 波形**。开火不只是发波形，还要把强度临时提上去、到点回落，
并且**永远以 App 回报的真实强度为准**：

- App 会**限幅**：我们发的 ``+10`` 可能实际只生效 ``+3``；
- App 有**舒适上限**与**绝对上限**两级，且上限会**随使用时间动态变化**（不能硬编码）；
- 因此本地只维护一份**最近已知真实强度**缓存，判定引擎按**真实值**决策。

这些状态全部来自 App 的**事件推送**（**不轮询**，§5.9.4）：

============================  ==========================================================
来源事件                        取什么
============================  ==========================================================
``props.intensityA/B``        通道**真实强度**（App 实际生效的值，不是我们发出去的值）
``slotState.channelA/B``      ``intensityMax``（当前有效上限）、``comfortLimit``（舒适/绝对上限）
============================  ==========================================================

SDK 已经把 ``devices.snapshot`` / ``devices.patch`` / ``slots.patch`` 合并进
``V4Client.devices``，并在每次合并成功后 emit ``devices`` 事件
（见 ``reference/dglab-kit-python/src/dglab_kit_python/socket/v4.py`` 的
``_handle_message_frame`` → ``client.dispatch(data)`` → ``emit("devices", ...)``）。

因此本模块只需要两个入口：

- :meth:`IntensityBook.apply_devices` —— 喂 SDK ``devices`` 事件的**全量**设备列表；
- :meth:`IntensityBook.apply_message` —— 直接喂原始帧（``slots.patch`` / ``devices.patch`` /
  ``devices.snapshot`` / ``devices.get`` 响应），方便单测与旁路接入。

设计
----

- 以 ``(client_id, slot_id)`` 为键，保存一份**深合并后的设备字典**（``props`` + ``slotState``）；
- :class:`ChannelState` 是某通道的**只读视图**；
- :meth:`IntensityBook.strength` / :meth:`IntensityBook.limit` 给开火与判定引擎读取。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ChannelState",
    "IntensityBook",
    "HARD_MAX_INTENSITY",
]

logger = logging.getLogger("coyotelink.coyote_v4.intensity")

#: 协议层硬上限（V4 强度值域）。这是**协议**边界，不是 App 的舒适上限 ——
#: 后者是运行时状态，从 ``slotState.channelX.comfortLimit`` 读，**不要硬编码**（§5.9.3）。
HARD_MAX_INTENSITY = 200

#: 通道号 → ``props`` 里的强度字段名
_PROP_KEY = {0: "intensityA", 1: "intensityB"}
#: 通道号 → ``slotState`` 里的通道状态键
_STATE_KEY = {0: "channelA", 1: "channelB"}


def _channel_int(channel: Any) -> int:
    """通道 → ``0`` / ``1``（延迟导入 ``client.channel_int``，避免循环依赖）。"""
    from .client import channel_int

    return channel_int(channel)


def _as_int(value: Any) -> int | None:
    """``int`` / 整数值 ``float`` → ``int``；``bool`` 与非数字 → ``None``。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and float(value).is_integer():
        return int(value)
    return None


def _deep_merge(base: Any, patch: Any) -> Any:
    """与 SDK 的 ``_merge_patch`` 同语义：dict 递归合并，非 dict 直接覆盖。"""
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return patch
    out = dict(base)
    for key, value in patch.items():
        out[key] = _deep_merge(base.get(key), value)
    return out


@dataclass(frozen=True)
class ChannelState:
    """某 ``(client_id, slot_id, channel)`` 的**最近已知**真实状态（只读）。"""

    client_id: str
    slot_id: str
    channel: int
    #: 最近已知**真实强度**（``props.intensityA/B``）；从未收到过为 ``None``
    strength: int | None = None
    #: 当前有效上限（``slotState.channelX.intensityMax``）
    intensity_max: int | None = None
    #: 舒适上限（``slotState.channelX.comfortLimit.comfortMax``）
    comfort_max: int | None = None
    #: 绝对上限（``slotState.channelX.comfortLimit.absoluteMax``）
    absolute_max: int | None = None
    #: 通道是否静音（``slotState.channelX.isMuted``）
    muted: bool | None = None

    @property
    def channel_name(self) -> str:
        return "A" if self.channel == 0 else "B"

    @property
    def limit(self) -> int | None:
        """开火前对照用的**安全上限**：舒适上限优先，其次绝对上限，再次 ``intensityMax``。

        三者都未知 → ``None``（表示"还不知道上限"，此时不做限幅判断）。
        """
        for value in (self.comfort_max, self.absolute_max, self.intensity_max):
            if value is not None:
                return value
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "slot_id": self.slot_id,
            "channel": self.channel_name,
            "strength": self.strength,
            "limit": self.limit,
            "comfort_max": self.comfort_max,
            "absolute_max": self.absolute_max,
            "intensity_max": self.intensity_max,
            "muted": self.muted,
        }


class IntensityBook:
    """按 ``slotId`` 深合并 App 上报的设备状态，并暴露只读的强度/上限查询。

    线程/事件循环安全说明：只在事件循环线程里被 SDK 事件 handler 与开火路径读写，
    与 :class:`~agent.nodes.coyote_v4.client.CoyoteClient` 的既有约定一致（单线程）。
    """

    def __init__(self) -> None:
        self._devices: dict[tuple[str, str], dict[str, Any]] = {}

    # ──────────────────────────────────────────────────────────
    # 写入（全部由 App 事件驱动）
    # ──────────────────────────────────────────────────────────
    def apply_devices(self, client_id: str | None, devices: Any) -> int:
        """用 App 的**全量**设备列表替换某 client 的设备缓存（``devices`` 事件 / 快照）。

        返回写入的设备数。缺席的 ``slotId`` 会被移除。
        """
        if not isinstance(client_id, str) or not client_id:
            return 0
        if not isinstance(devices, (list, tuple)):
            return 0
        seen: set[str] = set()
        count = 0
        for device in devices:
            if not isinstance(device, dict):
                continue
            slot_id = device.get("slotId")
            if not isinstance(slot_id, str) or not slot_id:
                continue
            # 全量快照 → 直接替换（SDK 的 V4Client 已把增量合并好）
            self._devices[(client_id, slot_id)] = dict(device)
            seen.add(slot_id)
            count += 1
        for key in [k for k in self._devices if k[0] == client_id and k[1] not in seen]:
            del self._devices[key]
        return count

    def apply_message(self, client_id: str | None, msg: Any) -> int:
        """直接喂一帧原始消息（``slots.patch`` / ``devices.patch`` / ``devices.snapshot``）。

        便于单测与旁路接入；语义与 SDK 的 ``V4Client.dispatch`` 一致（深合并）。
        返回被触及的设备数。
        """
        if not isinstance(client_id, str) or not client_id or not isinstance(msg, dict):
            return 0
        if msg.get("t") == "ev":
            event = msg.get("ev")
            if event == "devices.snapshot":
                return self.apply_devices(client_id, msg.get("devices") or [])
            if event == "devices.patch":
                count = 0
                for device in msg.get("added") or []:
                    if self._merge_device(client_id, device):
                        count += 1
                for slot_id in msg.get("removed") or []:
                    if self._devices.pop((client_id, slot_id), None) is not None:
                        count += 1
                return count
            if event == "slots.patch":
                count = 0
                for slot in msg.get("slots") or []:
                    if not isinstance(slot, dict):
                        continue
                    slot_id = slot.get("slotId")
                    if not isinstance(slot_id, str) or not slot_id:
                        continue
                    patch = {"slotId": slot_id}
                    if "props" in slot:
                        patch["props"] = slot.get("props") or {}
                    if "slotState" in slot:
                        patch["slotState"] = slot.get("slotState") or {}
                    if self._merge_device(client_id, patch):
                        count += 1
                return count
            return 0
        # devices.get 的响应（{"devices": [...]}）或裸快照
        if isinstance(msg.get("devices"), list):
            return self.apply_devices(client_id, msg["devices"])
        return 0

    def _merge_device(self, client_id: str, device: Any) -> bool:
        if not isinstance(device, dict):
            return False
        slot_id = device.get("slotId")
        if not isinstance(slot_id, str) or not slot_id:
            return False
        key = (client_id, slot_id)
        self._devices[key] = _deep_merge(self._devices.get(key, {}), device)
        return True

    def forget(self, client_id: str) -> int:
        """App 断开 → 丢弃其设备状态（返回删除条数）。"""
        keys = [k for k in self._devices if k[0] == client_id]
        for key in keys:
            del self._devices[key]
        return len(keys)

    def clear(self) -> None:
        self._devices.clear()

    # ──────────────────────────────────────────────────────────
    # 读取（只读；判定引擎 / 开火用）
    # ──────────────────────────────────────────────────────────
    def get(self, client_id: str, slot_id: str, channel: Any = "A") -> ChannelState:
        ch = _channel_int(channel)
        device = self._devices.get((client_id, slot_id)) or {}
        props = device.get("props") or {}
        state = device.get("slotState") or {}
        channel_state = state.get(_STATE_KEY.get(ch, "")) or {}
        comfort = channel_state.get("comfortLimit") or {}
        muted = channel_state.get("isMuted")
        return ChannelState(
            client_id=client_id,
            slot_id=slot_id,
            channel=ch,
            strength=_as_int(props.get(_PROP_KEY.get(ch, ""))),
            intensity_max=_as_int(channel_state.get("intensityMax")),
            comfort_max=_as_int(comfort.get("comfortMax")),
            absolute_max=_as_int(comfort.get("absoluteMax")),
            muted=muted if isinstance(muted, bool) else None,
        )

    def strength(self, client_id: str, slot_id: str, channel: Any = "A") -> int | None:
        """最近已知**真实强度**；从未收到过为 ``None``。"""
        return self.get(client_id, slot_id, channel).strength

    def limit(self, client_id: str, slot_id: str, channel: Any = "A") -> int | None:
        """最近已知**上限**（舒适 > 绝对 > intensityMax）；都未知为 ``None``。"""
        return self.get(client_id, slot_id, channel).limit

    def slots(self, client_id: str) -> list[str]:
        """某 client 当前已知的 ``slotId`` 列表。"""
        return [slot for (cid, slot) in self._devices if cid == client_id]

    def __len__(self) -> int:
        return len(self._devices)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, tuple) and len(key) == 2:
            return key in self._devices
        if isinstance(key, str):
            return any(cid == key for (cid, _slot) in self._devices)
        return False

    def as_dict(self) -> dict[str, Any]:
        """调试用：``{client_id: {slot_id: {A: ChannelState.to_dict(), B: ...}}}``。"""
        out: dict[str, Any] = {}
        for (client_id, slot_id) in self._devices:
            out.setdefault(client_id, {})[slot_id] = {
                "A": self.get(client_id, slot_id, "A").to_dict(),
                "B": self.get(client_id, slot_id, "B").to_dict(),
            }
        return out
