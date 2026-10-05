"""``device`` 节点 —— 设备枚举与热切换（M0，发 ``event/device``）。

职责
----
- 用 :class:`agent.core.devices.DeviceRegistry` 枚举本机全部摄像头 / 麦克风（去重、
  稳定 ID、回环标注）；
- 发 ``event/device``：设备快照（名字 + 稳定 ID + 选中项 + 错误）；
- 订阅 ``command/select_device``：``{"kind":"camera","id":...}`` 记住选择；
  ``{"action":"list"}`` 立刻重发一次快照（晚加入的前端拉取用）。

热插拔：``start_watch(5s)`` 定时重枚举，变化时自动推 ``event/device``。

它只"记账"（把选择写进 ``data/devices.json``）；真正打开设备的是 camera / audio 节点，
它们也订阅 ``command/select_device`` 自己动手切换 —— 这就是节点化的解耦。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from agent.core import Node, topics
from agent.core.devices import CAMERA, MICROPHONE, DeviceInfo, DeviceRegistry

__all__ = ["DeviceNode", "NODE"]


class DeviceNode(Node):
    name = "device"

    #: 热插拔轮询间隔（秒）
    watch_interval = 5.0
    #: 快照周期重发间隔（秒）——让晚连上的前端也能拿到列表
    republish_interval = 10.0

    def __init__(self, config: Any = None, *, registry: DeviceRegistry | None = None) -> None:
        super().__init__(config)
        self._registry = registry
        self._worker: asyncio.Task[Any] | None = None

    # ── 生命周期 ──────────────────────────────────────────────
    async def setup(self) -> None:
        if self._registry is None:
            self._registry = DeviceRegistry(logger=self.log)  # 构造即枚举
        self._pub = self.publish(topics.EVENT_DEVICE)
        self.subscribe(topics.COMMAND_SELECT_DEVICE, self.on_select)
        self._registry.on_change(self._on_change)
        self._registry.start_watch(self.watch_interval)
        self._publish_snapshot(reason="startup")
        self._worker = asyncio.create_task(self._periodic())

    async def teardown(self) -> None:
        task, self._worker = self._worker, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._registry is not None:
            self._registry.stop_watch()

    async def _periodic(self) -> None:
        while True:
            await asyncio.sleep(self.republish_interval)
            try:
                self._publish_snapshot(reason="periodic")
            except Exception as exc:  # noqa: BLE001
                # ⚠️ 绝不能让它抛出去：这个 while 是本节点**唯一**的"定期重发"来源，
                #    异常一旦逃逸，任务就静默死了 —— 面板此后永远收不到设备列表，
                #    而节点进程还活着、日志也没有报错（实测就是"摄像头/麦克风不识别"）。
                self.log.warning("设备快照定期发布失败（已忽略，下一轮重试）：%s", exc)

    # ── 快照 ──────────────────────────────────────────────────
    def _publish_snapshot(self, *, reason: str) -> None:
        if self._registry is None:
            return
        snapshot = self._registry.state()
        snapshot["ts"] = int(time.time() * 1000)
        snapshot["reason"] = reason
        # ★ 回环 / 输出设备（"立体声混音"、"电脑扬声器"）不进 UI 列表：
        #   它们不是麦克风，列出来只会让人选错（用户要求："不要置灰，直接屏蔽"）。
        #   数据照发（`hidden` 标记），排障时还看得见。
        for device in snapshot.get("devices") or []:
            detail = device.get("detail") or {}
            if detail.get("loopback") or detail.get("loopback_device"):
                device["hidden"] = True
        # 分类计数便于前端一眼看清（设备名可能含中文 / ®）
        snapshot["counts"] = {
            CAMERA: len(self._registry.list(CAMERA)),
            MICROPHONE: len(self._registry.list(MICROPHONE)),
        }
        snapshot["serial"] = self._serial_snapshot()
        self._pub.put(snapshot)

    # ── 串口（ESP32 假郊狼主机）────────────────────────────────
    def _serial_snapshot(self) -> dict[str, Any]:
        """把串口也当成一种"设备"列出来给面板选。

        为什么塞在 device 节点：面板只要**一个**设备来源。串口不属于
        :class:`DeviceRegistry`（那是音视频/BLE 三件套），所以单独枚举后并进同一份快照，
        前端就不用为它单开一路订阅。

        ``id`` 用**裸串口名**（``COM18``）—— :mod:`agent.nodes.sim_coyote` 的
        ``on_select`` 就是按这个找口的，别加 ``serial:`` 之类前缀。
        """
        try:
            from agent.nodes.sim_coyote.esp32_link import list_serial_ports

            ports = list_serial_ports()
        except Exception as exc:  # noqa: BLE001 - 拿不到串口不该打死节点
            return {"items": [], "error": f"枚举串口失败：{exc}"}
        items: list[dict[str, Any]] = []
        for port in ports:
            label = port.label
            if port.is_bluetooth:
                label += "（蓝牙虚拟口 · 多半收不到字节）"
            items.append({
                "id": port.id,                  # ★ 裸串口名，前端原样发回
                "label": label,
                "name": port.id,
                "kind": "serial",
                "available": True,
                "bluetooth": bool(port.is_bluetooth),
                # ★ 蓝牙虚拟口**不进 UI 列表**（用户要求："不要置灰，直接屏蔽"）：
                #   它打开能成功却收不到字节，列出来只会让人选错。数据照发，
                #   前端按 `hidden` 跳过（要排障时还能在"调试"里看到）。
                "hidden": bool(port.is_bluetooth),
                "likely_esp32": bool(port.likely_esp32),
            })
        return {"items": items}

    def _on_change(self, devices: list[DeviceInfo]) -> None:
        # 由注册表的轮询线程调用；Zenoh 的 put 线程安全
        self.log.info("设备列表发生变化（%d 项），推送 event/device", len(devices))
        self._publish_snapshot(reason="change")

    # ── 选择指令 ──────────────────────────────────────────────
    def on_select(self, msg: Any) -> None:
        if not isinstance(msg, dict):
            return
        action = msg.get("action")
        kind = msg.get("kind")
        device_id = msg.get("id")
        if action == "list" or not device_id:
            self._publish_snapshot(reason="list")
            return
        if kind == "serial":
            # 串口不在 DeviceRegistry 里（它管音视频/BLE），只记账 + 重发快照；
            # 真正的切换由 sim_coyote 节点自己做（它也订阅了这条指令）。
            self.log.info("串口选择：%s（转交 sim_coyote 热切换）", device_id)
        elif kind:
            self._registry.set_selected(kind, str(device_id))
            self.log.info("记住设备选择：%s = %s", kind, device_id)
        self._publish_snapshot(reason="select")


NODE = DeviceNode

if __name__ == "__main__":  # pragma: no cover - 便于 `python -m agent.nodes.device`
    raise SystemExit(DeviceNode.main())
