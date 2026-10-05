"""``heart`` 节点（T7，第一部分）—— 蓝牙心率带。

协议（标准 BLE，不需要厂商文档）：

* 服务 ``0x180D`` Heart Rate
* 特征 ``0x2A37`` Heart Rate Measurement（**Notify**）→ 心率 BPM，可选 RR 间期 / 接触检测
* 可选 ``0x180F/0x2A19`` Battery Level

对外契约：

* **发布** ``feature/heart``（消息类 :class:`agent.core.messages.FeatureHeart`）
* **订阅** 无

**设备选择 / 热切换**：走节点参数（``address`` 直连 / ``name_hint`` 名字匹配 / 否则自动扫描取
信号最强的一台），并且**订阅 ``command/select_device``**（M0 通用设备层，与 camera/heart/
sim_coyote 同一套约定）：

* ``{"kind":"heart","id":"AA:BB:CC:DD:EE:FF"}`` → 切到指定心率带（**不重启节点**）
* ``{"kind":"heart","id":""}`` / 不给 id → 回到**自动扫描**
* ``{"kind":"heart","action":"list"}`` → 把扫到的心率带打进日志（给人看）

切换动作 = 打断当前会话 → 按新目标重连（旧连接会正常 stop_notify 再断开）。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from agent.core import Node, topics
from agent.core.messages import FeatureHeart

__all__ = ["HeartNode", "parse_heart_rate_measurement", "HRS_SERVICE", "HRM_CHAR"]

HRS_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HRM_CHAR = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY_SERVICE = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_CHAR = "00002a19-0000-1000-8000-00805f9b34fb"

#: 断线重连退避上限（秒）
_MAX_BACKOFF = 30.0
#: `event/link` 重播间隔（秒）—— 面板晚开也能看到状态与候选心率带
#: ⚠️ 满负载时面板进程只收到约 10%（实测），所以重播要勤。
_LINK_REPEAT_S = 1.0


def parse_heart_rate_measurement(data: bytes | bytearray | memoryview) -> dict[str, Any]:
    """解析 ``0x2A37`` 的 payload —— **严格照 BLE 标准逐位解析**。

    flags 字节（bit0 起）::

        bit0  心率值为 uint16（0 = uint8）
        bit1  接触检测：0=不支持/未检测, 1=已检测
        bit2  是否支持接触检测
        bit3  是否有能量消耗字段（uint16，跳过）
        bit4  是否有 RR 间期数组（每个 uint16，单位 1/1024 秒）

    :returns: ``{"bpm": int, "contact": bool | None, "rr_ms": list[float]}``
    """
    b = bytes(data)
    if not b:
        raise ValueError("空的 Heart Rate Measurement")
    flags = b[0]
    i = 1
    if flags & 0x01:
        if len(b) < i + 2:
            raise ValueError("心率字段不完整（uint16）")
        bpm = int.from_bytes(b[i : i + 2], "little")
        i += 2
    else:
        if len(b) < i + 1:
            raise ValueError("心率字段不完整（uint8）")
        bpm = b[i]
        i += 1

    out: dict[str, Any] = {"bpm": bpm, "contact": None, "rr_ms": []}
    if flags & 0x04:  # bit2：支持接触检测 → bit1 才有意义
        out["contact"] = bool(flags & 0x02)
    if flags & 0x08:  # bit3：能量消耗（uint16）
        i += 2
    if flags & 0x10:  # bit4：RR 间期数组
        while i + 2 <= len(b):
            raw = int.from_bytes(b[i : i + 2], "little")
            out["rr_ms"].append(round(raw * 1000.0 / 1024.0, 1))
            i += 2
    return out


class HeartNode(Node):
    """蓝牙心率带节点。

    :param address: 直连指定设备地址（如 ``"AA:BB:CC:DD:EE:FF"``）；给了就不扫描
    :param name_hint: 名字包含该子串的设备优先（大小写不敏感）
    :param scan_timeout: 单次扫描时长（秒）
    :param stale_after: 多久没数据就判定失联（秒）
    :param auto_connect: 是否自动开始连接循环
    """

    name = "heart"

    def __init__(
        self,
        config: Any | None = None,
        *,
        address: str | None = None,
        name_hint: str = "",
        scan_timeout: float = 8.0,
        stale_after: float = 5.0,
        auto_connect: bool = True,
    ) -> None:
        super().__init__(config)
        self._address = address
        self._name_hint = name_hint
        self._scan_timeout = scan_timeout
        self._stale_after = stale_after
        self._auto_connect = auto_connect

        self._pub: Any = None
        self._task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        #: 热切换用：set 之后当前会话主动断开，好按新目标重连
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._device_name = ""
        self._device_mac = ""
        #: 最近一次收到的心率（便于测试/状态查询）
        self.last: dict[str, Any] | None = None
        self.frames = 0
        #: 链路状态（面板上「心率带」那盏灯）：connected / scanning / error / idle
        self._state = "idle"
        self._detail = ""
        #: 最近一次扫到的心率带 `[(address, rssi, name)]` —— 面板的下拉列表用它
        self._candidates: list[tuple[str, int, str]] = []
        self._pub_link: Any = None
        self._link_task: asyncio.Task[Any] | None = None
        self._last_payload: dict[str, Any] | None = None

    # ──────────────────────────────────────────────────────
    # 生命周期
    # ──────────────────────────────────────────────────────
    async def setup(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._pub = self.publish(topics.FEATURE_HEART)
        self._pub_link = self.publish(topics.EVENT_LINK)    # ★ 面板上的「心率带」灯 + 候选列表
        self.subscribe(topics.COMMAND_SELECT_DEVICE, self.on_select)
        if self._auto_connect:
            self._task = asyncio.create_task(self._run_forever(), name="heart-loop")
        self._link_task = asyncio.create_task(self._link_loop(), name="heart-link")
        self.publish_link()
        self.log.info(
            "heart 就绪：address=%s name_hint=%r（发布 %s）",
            self._address or "(自动扫描)", self._name_hint, topics.FEATURE_HEART,
        )

    async def teardown(self) -> None:
        self._stop.set()
        for attr in ("_link_task", "_task"):
            task = getattr(self, attr, None)
            setattr(self, attr, None)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    # ──────────────────────────────────────────────────────
    # 链路状态（面板：「心率带」那盏灯 + 可用设备列表）
    # ──────────────────────────────────────────────────────
    async def _link_loop(self) -> None:
        """周期重播：面板晚开也能看到状态与候选列表（Zenoh 首帧会丢）。"""
        while True:
            await asyncio.sleep(_LINK_REPEAT_S)
            self.publish_link()

    def publish_link(self) -> None:
        """发一条 ``event/link``（``kind="heart"``）：状态 + **可用心率带列表**。

        为什么要有它：以前心率带只在日志里（``action:"list"`` 也只是打日志），
        面板上既看不到"有没有扫到"，也没法选 —— 用户的原话是"列出心率带可用[设备]"。
        ``candidates`` 就是给面板下拉用的（`id` 用 **BLE 地址**，与 `select_device` 同格式）。
        """
        pub = self._pub_link
        if pub is None:
            return
        target = self._address or ""
        if self._state == "connected":
            detail = f"已连接 {self._device_name or self._device_mac}"
            if self.last and self.last.get("bpm"):
                detail += f"（{self.last['bpm']} BPM）"
            hint = ""
        elif self._state == "scanning":
            detail = self._detail or "正在扫描附近的心率带…"
            hint = "心率带要先开机配对模式（多数是戴上就广播）"
        elif self._state == "error":
            detail = self._detail or "心率带连接出错"
            hint = "看 logs\\heart.log；心脏带没电/被别的手机占用都会这样"
        else:
            detail = self._detail or "未连接"
            hint = "点「重新扫描」列出附近的心率带，选一台再点「连接」"
        payload = {
            "ts": int(time.time() * 1000),
            "source": self.name,
            "kind": "heart",
            "state": self._state,
            "device": self._device_name or self._device_mac,
            "address": target,
            "auto": not target,
            "bpm": (self.last or {}).get("bpm"),
            "candidates": [
                {"id": address, "name": name or address, "rssi": rssi}
                for address, rssi, name in self._candidates
            ],
            "detail": detail,
            "hint": hint,
        }
        self._last_payload = payload
        pub.put(payload)

    def _set_state(self, state: str, detail: str = "") -> None:
        if state == self._state and detail == self._detail:
            return
        self._state, self._detail = state, detail
        self.publish_link()

    @property
    def device_name(self) -> str:
        return self._device_name

    # ──────────────────────────────────────────────────────
    # 设备选择 / 热切换（M0 通用设备层）
    # ──────────────────────────────────────────────────────
    def on_select(self, msg: Any) -> None:
        """``command/select_device`` 回调 —— 按 ``kind`` 换心率带并**热切换**。

        ⚠️ Zenoh 订阅回调**不在事件循环线程**上，所以真正的动作经
        ``call_soon_threadsafe`` 送回事件循环（同线程时直接建 task，便于单测）。
        """
        if not isinstance(msg, dict):
            return
        kind = msg.get("kind")
        if kind not in (None, "heart"):
            return  # 别人的设备指令（camera / sim_coyote / …）
        if msg.get("action") == "list":
            self._spawn(self._log_candidates())
            return
        address = str(msg.get("id") or msg.get("address") or "").strip().upper()
        self._spawn(self._switch(address))

    async def _switch(self, address: str) -> None:
        """换目标：``""`` = 回到自动扫描；否则直连该地址。"""
        old = self._address or "(自动扫描)"
        new = address or "(自动扫描)"
        if new == old:
            self.log.info("心率带未变（%s），忽略本次选择", new)
            return
        self.log.info("心率带热切换：%s → %s", old, new)
        self._address = address or None
        self._wake.set()  # 让当前会话断开，连接循环会按新目标重连

    @property
    def address(self) -> str | None:
        """当前目标地址（``None`` = 自动扫描）。给测试与 /status 用。"""
        return self._address

    async def _log_candidates(self) -> None:
        """扫一遍并把结果**发给面板**（``action:"list"`` 现在等于"重新扫描"）。

        ⚠️ 以前这里只 `log.info`，面板什么也看不到 —— 所以前端没法"列出可用心率带"。
        """
        self._set_state("scanning", "正在扫描附近的心率带…")
        try:
            found = await self._scan_candidates()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("扫描心率带失败：%s", exc)
            self._set_state("error", f"扫描失败：{exc}")
            return
        self._candidates = found
        self.publish_link()
        if not found:
            self.log.info("没扫到心率带")
            self._set_state(
                self._state if self._state == "connected" else "idle",
                "没扫到心率带（戴上/开配对模式后再点「重新扫描」）",
            )
            return
        for address, rssi, name in found:
            self.log.info("  候选：%s  rssi=%d  name=%r", address, rssi, name)
        self._set_state(
            self._state if self._state == "connected" else "idle",
            f"扫到 {len(found)} 台心率带，可在面板上选一台连接",
        )

    async def _scan_candidates(self) -> list[tuple[str, int, str]]:
        """扫一遍，返回**所有**像心率带的目标（按信号强→弱）。"""
        from bleak import BleakScanner  # noqa: PLC0415

        found = await BleakScanner.discover(timeout=self._scan_timeout, return_adv=True)
        out: list[tuple[str, int, str]] = []
        for address, (dev, adv) in found.items():
            name = dev.name or adv.local_name or ""
            uuids = {str(u).lower() for u in (adv.service_uuids or [])}
            hit = HRS_SERVICE in uuids or bool(
                self._name_hint and self._name_hint.lower() in name.lower()
            )
            if hit:
                out.append((address, adv.rssi if adv.rssi is not None else -999, name))
        return sorted(out, key=lambda row: -row[1])

    def _spawn(self, coro: Any) -> None:
        """把协程送回事件循环执行（订阅回调可能在别的线程）。"""
        loop = self._loop
        if loop is None:
            return
        try:
            if asyncio.get_running_loop() is loop:
                asyncio.ensure_future(coro)
                return
        except RuntimeError:
            pass
        asyncio.run_coroutine_threadsafe(coro, loop)

    # ──────────────────────────────────────────────────────
    # 连接循环
    # ──────────────────────────────────────────────────────
    async def _run_forever(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                if not self._address:
                    self._set_state("scanning", "正在扫描附近的心率带…")
                address = self._address or await self._discover()
                if not address:
                    self.log.info("没找到心率带，%.0fs 后重试", backoff)
                    self._set_state("idle", "没找到心率带（戴上/开配对模式后再试）")
                    await self._pause(backoff)
                    backoff = min(backoff * 2, _MAX_BACKOFF)
                    continue
                await self._session(address)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 断线/找不到设备都不能打死节点
                self.log.warning("心率带会话出错（%.0fs 后重试）：%s", backoff, exc)
                self._set_state("error", f"会话出错（{backoff:.0f}s 后重试）：{exc}")
                await self._pause(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
        self._set_state("idle", "已停止")

    async def _pause(self, seconds: float) -> None:
        """可被 stop 打断的 sleep。"""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def _discover(self) -> str | None:
        """扫 BLE，挑一台心率带（优先广播里带 HRS 服务 UUID 的，退化到名字匹配）。"""
        from bleak import BleakScanner  # noqa: PLC0415 - 延迟导入，没装 bleak 也能 import 本模块

        found = await BleakScanner.discover(timeout=self._scan_timeout, return_adv=True)
        best: tuple[str, int, str] | None = None
        for address, (dev, adv) in found.items():
            name = dev.name or adv.local_name or ""
            uuids = {str(u).lower() for u in (adv.service_uuids or [])}
            hit = HRS_SERVICE in uuids or bool(
                self._name_hint and self._name_hint.lower() in name.lower()
            )
            if not hit:
                continue
            rssi = adv.rssi if adv.rssi is not None else -999
            if best is None or rssi > best[1]:
                best = (address, rssi, name)
        if best is None:
            return None
        self.log.info("找到心率带：name=%r addr=%s rssi=%d", best[2], best[0], best[1])
        self._candidates = [
            (address, adv.rssi if adv.rssi is not None else -999, dev.name or adv.local_name or "")
            for address, (dev, adv) in found.items()
            if HRS_SERVICE in {str(u).lower() for u in (adv.service_uuids or [])}
            or bool(self._name_hint and self._name_hint.lower() in (dev.name or adv.local_name or "").lower())
        ]
        self._candidates.sort(key=lambda row: -row[1])
        self.publish_link()          # 顺手把"扫到哪些"发给面板
        return best[0]

    async def _session(self, address: str) -> None:
        from bleak import BleakClient  # noqa: PLC0415

        self.log.info("连接心率带 %s …", address)
        self._set_state("scanning", f"正在连接 {address} …")
        async with BleakClient(address, timeout=20) as client:
            self.log.info("已连接 %s（mtu=%s）", address, getattr(client, "mtu_size", "?"))
            self._device_mac = address
            self._device_name = self._name_of(client, address)
            self._set_state("connected", f"已连接 {self._device_name or address}")
            try:
                await client.start_notify(HRM_CHAR, self._on_measurement)
            except Exception as exc:  # noqa: BLE001
                self.log.error("订阅 0x2A37 失败：%s", exc)
                raise
            await self._log_battery(client)
            while (not self._stop.is_set()
                   and client.is_connected
                   and not self._wake.is_set()):
                await self._pause(1.0)
            self._wake.clear()  # 热切换：断开旧连接，外层循环按新目标重连
            with contextlib.suppress(Exception):
                await client.stop_notify(HRM_CHAR)
        self.log.info("心率带已断开 %s", address)
        self._device_mac = ""
        self._device_name = ""
        self._set_state("idle", f"已断开 {address}（按新目标重连中）")

    @staticmethod
    def _name_of(client: Any, fallback: str) -> str:
        for getter in ("name",):
            try:
                v = getattr(client, getter, None)
                if isinstance(v, str) and v:
                    return v
            except Exception:  # noqa: BLE001
                pass
        return fallback

    async def _log_battery(self, client: Any) -> None:
        try:
            raw = await client.read_gatt_char(BATTERY_CHAR)
            self.log.info("心率带电量 = %d%%", int(bytes(raw)[0]))
        except Exception:  # noqa: BLE001 - 很多心率带没有电量服务
            self.log.debug("读取电量失败（设备可能不支持 0x180F）")

    # ──────────────────────────────────────────────────────
    # 数据回调（可能在 bleak 的后台线程）
    # ──────────────────────────────────────────────────────
    def _on_measurement(self, _sender: Any, data: bytearray) -> None:
        try:
            parsed = parse_heart_rate_measurement(data)
        except ValueError as exc:
            self.log.debug("心率帧解析失败（%s）：%s", bytes(data).hex(" "), exc)
            return
        self.frames += 1
        msg = FeatureHeart(
            ts=time.time() * 1000.0,
            bpm=parsed["bpm"],
            contact=parsed["contact"],
            rr_ms=parsed["rr_ms"] or None,
            device=self._device_name,
            mac=self._device_mac,
        )
        self.last = msg.to_dict()
        self._publish(msg)

    def _publish(self, msg: FeatureHeart) -> None:
        """统一在事件循环线程上 publish（订阅/回调线程不一定是主线程）。"""
        loop = self._loop

        def _do() -> None:
            if self._pub is not None:
                self._pub.put(msg)

        if loop is None:
            return
        try:
            if asyncio.get_running_loop() is loop:
                _do()
                return
        except RuntimeError:
            pass
        loop.call_soon_threadsafe(_do)
