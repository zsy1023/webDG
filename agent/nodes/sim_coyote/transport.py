"""sim_coyote 的 BLE 外设后端 —— **可插拔**，让协议/节点逻辑与具体硬件解耦。

背景（2026-10-03 实测结论，详见 T1 任务书 §8）：

    Windows 的 ``GattServiceProvider`` **不能发布保留服务**
    （``0x180A``/``0x1800``/``0x1801`` → ``DISABLED_BY_POLICY``，系统设计使然、无解），
    且广播名只能是系统蓝牙名。所以 **"Windows 纯软件冒充郊狼主机"走不通**，
    改为 **ESP32 跑真 BLE 外设 + 与主机通信**。

======================  ============================================================
后端                     用途
======================  ============================================================
:class:`Esp32Peripheral` ★ **现行路线**：ESP32 复刻真机 GATT，经串口/TCP 与主机通信
:class:`WinRtPeripheral` Windows 原生 ``GattServiceProvider`` —— **受限、非交付路线**，
                         仅留作参考/回归对照
:class:`LoopbackPeripheral` 无硬件自测：手工喂字节进协议层（单测、联调用）
======================  ============================================================

后端只管三件事：``start()`` / ``stop()`` / ``notify(data)``，并在收到写入时
回调 ``on_write(data)``。**后端不认识 B0/BF** —— 那是 :mod:`agent.nodes.sim_coyote.protocol` 的事。
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import esp32_link
from . import protocol as P

__all__ = [
    "PeripheralError",
    "ClientInfo",
    "PeripheralBackend",
    "Esp32Peripheral",
    "WinRtPeripheral",
    "LoopbackPeripheral",
    "create_backend",
]

log = logging.getLogger("coyotelink.sim_coyote.transport")


class PeripheralError(RuntimeError):
    """后端启动 / 运行失败。"""


@dataclass
class ClientInfo:
    """连上假主机的"App 是谁"—— 需求 7「最好能看到是谁访问了」的一环。"""

    backend: str
    device_id: str = ""
    device_name: str = ""
    peer_mac: str = ""
    connected_at: float = 0.0
    connected: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "peer_mac": self.peer_mac,
            "connected_at": self.connected_at,
            "connected": self.connected,
            "extra": self.extra,
        }


class PeripheralBackend(abc.ABC):
    """假主机传输后端的统一接口。

    :param on_write: 收到 App 写入的**原始字节**时回调（同步，在事件循环线程里）
    :param on_connect: 连接状态变化时回调 ``on_connect(ClientInfo | None)``
    :param name: 对外广播名（真机是 ``47L121000``）
    """

    kind: str = "abstract"

    def __init__(
        self,
        *,
        on_write: Callable[[bytes], None] | None = None,
        on_connect: Callable[[ClientInfo | None], None] | None = None,
        name: str = P.DEVICE_NAME_COYOTE,
        **extra: Any,
    ) -> None:
        if extra:
            # 后端用不到的参数（典型：给 esp32 配的 transport/port 被一并传给了 loopback）——
            # 记一笔，别静默吞掉拼写错误
            log.debug("%s 忽略无关参数：%s", type(self).__name__, sorted(extra))
        self.name = name
        self._on_write = on_write
        self._on_connect = on_connect
        self._client: ClientInfo | None = None

    # ── 生命周期 ──────────────────────────────────────────
    @abc.abstractmethod
    async def start(self) -> None:
        """创建服务 / 打开链路 / 开始广播。"""

    @abc.abstractmethod
    async def stop(self) -> None:
        """停止广播 / 关闭链路。"""

    @abc.abstractmethod
    async def notify(self, data: bytes) -> int:
        """把一帧字节推给已订阅的 App，返回实际投递的订阅者数。"""

    # ── 状态 ──────────────────────────────────────────────
    @property
    def is_connected(self) -> bool:
        return bool(self._client and self._client.connected)

    @property
    def client_info(self) -> ClientInfo | None:
        return self._client

    # ── 给子类用 ──────────────────────────────────────────
    def _emit_write(self, data: bytes) -> None:
        if self._on_write is None:
            return
        try:
            self._on_write(bytes(data))
        except Exception:  # noqa: BLE001 - 回调出错不能把后端打崩
            log.exception("on_write 回调出错")

    def _emit_connect(self, info: "ClientInfo | None") -> None:
        self._client = info
        if self._on_connect is None:
            return
        try:
            self._on_connect(info)
        except Exception:  # noqa: BLE001
            log.exception("on_connect 回调出错")


# ══════════════════════════════════════════════════════════════
# ★ 现行路线：ESP32
# ══════════════════════════════════════════════════════════════
class Esp32Peripheral(PeripheralBackend):
    """ESP32 硬件外设链路（**现行路线**）。

    ESP32 侧用 NimBLE 复刻真机 GATT（含 ``0x180A``/``0x1800`` 与广播名 ``47L121000``），
    本类只负责"主机 ⇄ ESP32"的字节收发。

    链路设计（**给 ESP32 固件实现方的接口契约**）::

        ┌─ ESP32 → 主机（上行，App 的指令 + 连接事件）───────────────┐
        │ 0xB0 + 19B   B0 指令（20 字节，原样透传）                 │
        │ 0xBF +  6B   BF 指令（7 字节，原样透传）                  │
        │ 0xCC         App 已连上                                   │
        │ 0xCD         App 已断开                                   │
        └───────────────────────────────────────────────────────────┘
        ┌─ 主机 → ESP32（下行，我们回给 App 的帧）──────────────────┐
        │ 0xB1 + 3B    B1 强度回包（4 字节）                        │
        │ 0xCE         心跳（ESP32 侧可忽略）                       │
        └───────────────────────────────────────────────────────────┘

    即**直接用 HEAD 字节区分帧型**，不需要长度前缀（B0=20 / BF=7 / B1=4 都是定长）。

    :param transport: ``"serial"``（串口，**已落地**）或 ``"tcp"``（WiFi，尚未实现）
    :param port: 串口名（如 ``COM18``）；``""`` / ``"auto"`` → 自动挑一个像 ESP32 的口。
        配置入口在 ``agent/config.toml`` 的 ``[sim_coyote] port``，也可以被
        ``command/select_device`` 热切换（见 ``node.py`` 的 ``on_select``）
    :param baudrate: 默认 115200（8N1），与固件一致

    实现说明（2026-10-04，串口链路落地）:

    * 串口细节（DTR/RTS 防复位、收字节线程、列可用串口）在 :mod:`.esp32_link`；
    * ``start()`` 打开串口后会**在线探测**（发 0xCE，等固件回帧/日志行）——
      「打开成功但其实没有假主机」（典型：填了蓝牙虚拟串口）会**明确报错**，不静默假死；
    * 链路断了会自动退避重连（拔线/板子复位/端口被抢占都算）；
    * 固件日志统一 ``#`` 前缀：本类在分帧时**整行丢弃**，不会污染帧流（见 :meth:`_decode_uplink`）。
    """

    kind = "esp32"

    #: 上行帧长（HEAD → 总长）
    UPLINK_LEN = {P.HEAD_B0: P.B0_LEN, P.HEAD_BF: P.BF_LEN, 0xCC: 1, 0xCD: 1}
    #: 下行
    DOWNLINK_HEARTBEAT = 0xCE

    #: 在线探测超时（秒）：发 0xCE 后这么久内没有任何假主机应答 → 判定"这条线上没有假主机"
    probe_timeout = 2.0
    #: 断线重连退避（秒），用满后停在最后一档
    reconnect_backoff = (1.0, 2.0, 3.0, 5.0, 8.0, 10.0)
    #: 上行缓冲上限（字节）：超过就清空重同步（丢字节导致错位时的兜底）
    buffer_limit = 4096

    def __init__(
        self,
        *,
        transport: str = "serial",
        port: str = "COM5",
        baudrate: int = 115200,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.transport = transport
        self.port = port
        self.baudrate = baudrate
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task[Any] | None = None
        self._link: esp32_link.SerialLink | None = None
        self._buf = bytearray()
        #: "线另一端确实是假主机固件"的证据（收到帧或固件日志行）—— 在线探测用
        self._alive = asyncio.Event()
        self._reconnect: asyncio.Task[Any] | None = None
        self._stopping = False
        #: 是否已经**成功启动过**（探测期间断链不该触发重连 —— 那时还没有链路可重建）
        self._started = False
        #: 链路统计（排障用：``garbage`` 持续增长往往意味着丢字节后错位）
        self.stats: dict[str, int] = {
            "bytes": 0, "frames": 0, "log_lines": 0, "garbage": 0, "reconnects": 0,
        }

    # ── 生命周期 ──────────────────────────────────────────
    async def start(self) -> None:
        """打开串口 → **在线探测** → 开始收字节。失败抛 :class:`PeripheralError`。"""
        self._stopping = False
        if self.transport != "serial":
            raise PeripheralError(
                f"Esp32Peripheral 目前只有串口链路（transport={self.transport!r}）——"
                "WiFi/TCP 见 T1 任务书 §8.2 的 ②，尚未落地。"
            )
        port = self._resolve_port()
        self.port = port
        link = self._new_link(port)
        try:
            link.open()
        except esp32_link.LinkError as exc:
            raise PeripheralError(f"ESP32 假主机链路：{exc}") from exc
        self._link = link
        await self._probe(link)
        self._buf.clear()
        self._started = True
        log.info("ESP32 链路就绪：%s @ %d 8N1（假主机在线）", port, self.baudrate)

    async def stop(self) -> None:
        self._stopping = True
        self._started = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
        reconn, self._reconnect = self._reconnect, None
        if reconn is not None:
            reconn.cancel()
            try:
                await reconn
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        link, self._link = self._link, None
        if link is not None:
            link.close()
        self._buf.clear()
        self._alive.clear()

    async def notify(self, data: bytes) -> int:
        """把一帧（B1）写进串口交给 ESP32。链路没开返回 0。"""
        link = self._link
        if link is None:
            return 0
        return 1 if link.write(bytes(data)) else 0

    @property
    def link_stats(self) -> dict[str, Any]:
        """链路自检信息（日志 / 排障用）。"""
        return {
            "transport": self.transport,
            "port": self.port,
            "baudrate": self.baudrate,
            "link_open": bool(self._link and self._link.is_open),
            **self.stats,
        }

    # ── 链路内部 ──────────────────────────────────────────
    def _new_link(self, port: str) -> esp32_link.SerialLink:
        return esp32_link.SerialLink(
            port,
            self.baudrate,
            loop=asyncio.get_running_loop(),
            on_bytes=self._on_bytes,
            on_closed=self._on_link_closed,
        )

    def _resolve_port(self) -> str:
        """``port`` 为空或 ``"auto"`` → 自动挑一个像 ESP32 的口（**跳过蓝牙虚拟口**）。"""
        port = str(self.port or "").strip()
        if port and port.lower() != "auto":
            return port
        ports = esp32_link.list_serial_ports()
        guess = esp32_link.pick_likely_esp32(ports)
        if not guess:
            raise PeripheralError(
                "port 未配置（或为 auto），且自动挑不到串口。可用串口："
                + (", ".join(p.label for p in ports) or "（一个都没有）")
                + "。请在 agent/config.toml 的 [sim_coyote] port 里显式填板子所在的串口。"
            )
        log.info("port=%r → 自动选中 %s", self.port, guess)
        return guess

    async def _probe(self, link: esp32_link.SerialLink) -> None:
        """在线探测：发一次下行心跳，等假主机应答。

        ★ 这是「错口 / 没插板子必须明确报错，不许静默假死」的落地 ——
        蓝牙虚拟串口（本机 COM5/COM6）**能打开成功却永远收不到字节**，
        只靠"打开成功"是分辨不出来的，必须靠"对面有没有应答"。
        """
        self._alive.clear()
        link.send_heartbeat()
        # 轮询等待（而不是干等满超时）：链路自己坏了就立刻收摊，别让 start() 白等
        deadline = time.monotonic() + self.probe_timeout
        while time.monotonic() < deadline:
            if self._alive.is_set():
                return
            if not link.is_open:
                break
            await asyncio.sleep(0.02)
        link.close()
        if self._link is link:
            self._link = None
        raise PeripheralError(
            f"ESP32 假主机在串口 {self.port} 上没有应答：串口打开成功，但没能确认假主机在线"
            "（发了 0xCE 心跳，没等到帧、也没等到固件日志行；链路自身出错也会走到这里）。"
            "常见原因：填的是蓝牙虚拟串口（能打开、收不到字节）、板子没烧固件、或板子掉线。"
            "列一下可用串口：python -m agent.nodes.sim_coyote.esp32_link"
        ) from None

    def _on_bytes(self, chunk: bytes) -> None:
        """链路回调（**事件循环线程**）：收字节 → 分帧 → 逐帧处理。"""
        self.stats["bytes"] += len(chunk)
        self._buf.extend(chunk)
        for frame in self._decode_uplink(self._buf):
            self.stats["frames"] += 1
            self._handle_uplink_frame(frame)

    def _on_link_closed(self, exc: BaseException) -> None:
        """链路断开（拔线 / 板子复位 / 端口被抢占）→ 上报断开 + 安排自动重连。"""
        if self._stopping or not self._started:
            # 还没成功启动过（例如在线探测期间写失败）→ 不排重连：
            # 这时没有"已建立的链路"可重建，重连任务只会在后台空转刷日志
            return
        log.warning("ESP32 串口链路断开：%s → 自动重连中", exc)
        self._emit_connect(None)  # App 状态未知，先按"断开"处理
        task = self._reconnect
        if task is None or task.done():
            self._reconnect = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """退避重连，直到成功或节点被停掉。"""
        self.stats["reconnects"] += 1
        attempt = 0
        while not self._stopping:
            delay = self.reconnect_backoff[min(attempt, len(self.reconnect_backoff) - 1)]
            await asyncio.sleep(delay)
            if self._stopping:
                return
            attempt += 1
            old, self._link = self._link, None
            if old is not None:
                old.close()
            link = self._new_link(self.port)
            try:
                link.open()
            except esp32_link.LinkError as exc:
                log.info("重连 %s 第 %d 次失败：%s", self.port, attempt, exc)
                continue
            self._link = link
            try:
                await self._probe(link)
            except PeripheralError as exc:
                log.info("重连 %s 第 %d 次：链路打开了但没等到假主机应答：%s", self.port, attempt, exc)
                continue
            self._buf.clear()
            log.info("★ ESP32 链路已重连：%s @ %d（第 %d 次尝试）", self.port, self.baudrate, attempt)
            return

    # ── 上行分帧（ESP32 → 主机）────────────────────────────
    def _decode_uplink(self, buf: bytearray) -> list[bytes]:
        """从字节流里切出完整帧；返回切出的帧列表，``buf`` 里留残余。

        按 HEAD 判长度 —— B0/BF/CC/CD 都是定长，不需要长度前缀。

        额外处理固件日志：固件把日志统一写成 ``#`` + 换行，这里**整行丢掉**
        （不进协议流、也不会被误判成帧）；而"收到日志行"本身就是假主机在线的证据。
        """
        frames: list[bytes] = []
        while buf:
            head = buf[0]
            if head == esp32_link.LOGLINE_PREFIX:
                end = buf.find(b"\n")
                if end < 0:
                    if len(buf) > 512:  # 半行异常长 → 丢弃，别让缓冲无限长
                        log.debug("固件日志行过长且无换行，丢弃 %d 字节", len(buf))
                        buf.clear()
                    break
                line = bytes(buf[:end]).decode("utf-8", "replace").rstrip("\r")
                del buf[: end + 1]
                self.stats["log_lines"] += 1
                # 固件日志原样进 PC 侧日志（排障要看它；'#' 前缀去掉）
                log.info("ESP32 固件 |%s", line[1:] if line.startswith("#") else line)
                self._note_alive()
                continue
            need = self.UPLINK_LEN.get(head)
            if need is None:
                # 未知字节：板子复位时 ROM 日志的残渣，或丢字节后的错位 —— 逐字节丢弃重找帧头
                self.stats["garbage"] += 1
                log.debug("ESP32 上行未知字节 0x%02X，丢弃", head)
                del buf[0]
                continue
            if len(buf) < need:
                break
            frames.append(bytes(buf[:need]))
            del buf[:need]
            self._note_alive()
        if len(buf) > self.buffer_limit:  # 兜底：任何情况下别让缓冲失控
            log.warning("ESP32 上行缓冲异常（%d 字节）→ 清空重同步", len(buf))
            buf.clear()
        return frames

    def _note_alive(self) -> None:
        """记录"线另一端确实是假主机固件"的证据（在线探测用）。"""
        if not self._alive.is_set():
            self._alive.set()

    def _handle_uplink_frame(self, frame: bytes) -> None:
        """把一帧上行帧翻译成 写入回调 / 连接事件。"""
        head = frame[0]
        if head == 0xCC:
            self._emit_connect(ClientInfo(backend=self.kind, connected=True, connected_at=time.time()))
            log.info("ESP32 上报：App 已连接")
        elif head == 0xCD:
            self._emit_connect(None)
            log.info("ESP32 上报：App 已断开")
        else:
            self._emit_write(frame)


# ══════════════════════════════════════════════════════════════
# 受限：Windows 原生
# ══════════════════════════════════════════════════════════════
class WinRtPeripheral(PeripheralBackend):
    """Windows 原生 ``GattServiceProvider``（**受限、非交付路线**）。

    只能发布 ``0x180C`` / ``0x2003`` / ``0x2004``；
    ``0x180A`` / ``0x1800`` / ``0x1801`` 会 ``DISABLED_BY_POLICY``，
    且广播名只能是系统蓝牙名。保留此实现仅为**参考与回归对照**。

    PyWinRT 的 import 放在 :meth:`start` 里，保证非 Windows 环境也能 import 本模块。
    """

    kind = "winrt"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._providers: list[Any] = []
        self._notify_char: Any = None
        self._warnings: list[str] = []
        self._loop: Any = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        from winrt.windows.devices.bluetooth import BluetoothAdapter  # noqa: PLC0415
        from winrt.windows.devices.bluetooth.genericattributeprofile import (  # noqa: PLC0415
            GattCharacteristicProperties as P_,
        )
        from winrt.windows.devices.bluetooth.genericattributeprofile import (  # noqa: PLC0415
            GattLocalCharacteristicParameters,
            GattServiceProvider,
            GattServiceProviderAdvertisingParameters,
        )

        adapter = await BluetoothAdapter.get_default_async()
        if adapter is None or not adapter.is_peripheral_role_supported:
            raise PeripheralError("蓝牙适配器不支持 BLE 外设角色")

        prov = await self._advertise_service(GattServiceProvider, 0x180C, [
            (0x150A, P_.WRITE_WITHOUT_RESPONSE, self._make_write_handler()),
            (0x150B, P_.NOTIFY, self._make_notify_char()),
        ], GattLocalCharacteristicParameters)
        self._providers.append(prov)

        for svc, chs in ((0x2003, [(0x0007, P_.WRITE), (0x0008, P_.READ | P_.NOTIFY)]),
                         (0x2004, [(0x0009, P_.READ | P_.WRITE)])):
            try:
                self._providers.append(
                    await self._advertise_service(GattServiceProvider, svc, chs, GattLocalCharacteristicParameters)
                )
            except PeripheralError as exc:
                self._warnings.append(str(exc))

        # 保留服务：预期失败，明确记录下来（别让后来人再踩一次）
        for svc in (0x180A, 0x1800, 0x1801):
            try:
                res = await GattServiceProvider.create_async(_uuid16(svc))
                if int(res.error) != 0:
                    self._warnings.append(f"0x{svc:04X} 发布失败：{res.error}（Windows 保留服务，无解）")
            except Exception as exc:  # noqa: BLE001
                self._warnings.append(f"0x{svc:04X} 异常：{exc}")

        adv = GattServiceProviderAdvertisingParameters()
        adv.is_discoverable = True
        adv.is_connectable = True
        for p in self._providers:
            p.start_advertising_with_parameters(adv)
        await asyncio.sleep(0.5)
        for w in self._warnings:
            log.warning("WinRtPeripheral：%s", w)

    async def stop(self) -> None:
        providers, self._providers = self._providers, []
        for p in providers:
            try:
                p.stop_advertising()
            except Exception:  # noqa: BLE001
                pass

    async def notify(self, data: bytes) -> int:
        if self._notify_char is None:
            return 0
        try:
            from winrt.windows.storage.streams import DataWriter  # noqa: PLC0415

            w = DataWriter()
            w.write_bytes(bytes(data))
            await self._notify_char.notify_value_async(w.detach_buffer())
            return 1
        except Exception as exc:  # noqa: BLE001
            log.warning("notify 失败：%s", exc)
            return 0

    # ── 内部 ─────────────────────────────────────────────
    async def _advertise_service(self, GattServiceProvider, short: int, chs, ParamsCls):
        res = await GattServiceProvider.create_async(_uuid16(short))
        if int(res.error) != 0:
            raise PeripheralError(f"0x{short:04X} 创建失败：{res.error}")
        prov = res.service_provider
        for cshort, props, hook in chs:
            params = ParamsCls()
            params.characteristic_properties = props
            rr = await prov.service.create_characteristic_async(_uuid16(cshort), params)
            if int(rr.error) != 0:
                raise PeripheralError(f"0x{short:04X}/0x{cshort:04X} 创建失败：{rr.error}")
            if hook is not None:
                hook(rr.characteristic)
        return prov

    def _make_write_handler(self):
        def hook(char: Any) -> None:
            char.add_write_requested(self._on_write_requested)
        return hook

    def _make_notify_char(self):
        def hook(char: Any) -> None:
            self._notify_char = char
            char.add_subscribed_clients_changed(self._on_subscribed)
        return hook

    def _on_write_requested(self, sender: Any, args: Any) -> None:
        """WinRT 后台线程回调 → 转交事件循环（probe 里踩过的坑：要 deferral）。"""
        try:
            deferral = args.get_deferral()
            loop = self._loop
            if loop is None:  # 还没 start 就收到写入 → 直接放行
                deferral.complete()
                return
            asyncio.run_coroutine_threadsafe(self._read_write(args, deferral), loop)
        except Exception:  # noqa: BLE001
            log.exception("处理写入请求失败")

    async def _read_write(self, args: Any, deferral: Any) -> None:
        try:
            req = await args.get_request_async()
            from winrt.windows.storage.streams import DataReader  # noqa: PLC0415

            buf = req.value
            data = bytes(DataReader.from_buffer(buf).read_bytes(buf.length))
            try:
                req.respond()
            except Exception:  # noqa: BLE001
                pass
            self._emit_write(data)
        except Exception:  # noqa: BLE001
            log.exception("读写入数据失败")
        finally:
            deferral.complete()

    def _on_subscribed(self, sender: Any, _args: Any) -> None:
        n = len(list(sender.subscribed_clients))
        log.info("WinRtPeripheral：0x150B 订阅数 = %d", n)
        if n:
            self._emit_connect(ClientInfo(backend=self.kind, connected=True, connected_at=time.time()))
        else:
            self._emit_connect(None)


# ══════════════════════════════════════════════════════════════
# 无硬件自测
# ══════════════════════════════════════════════════════════════
class LoopbackPeripheral(PeripheralBackend):
    """无硬件自测后端：手工把字节喂进协议层，并记录所有出站 notify。

    用途：单测 / 没有 ESP32 时把 ``SimCoyoteNode`` 整条逻辑跑通。
    """

    kind = "loopback"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sent: list[bytes] = []
        self.started = False

    async def start(self) -> None:
        self.started = True
        self._emit_connect(ClientInfo(backend=self.kind, device_name="loopback", connected=True,
                                      connected_at=time.time()))

    async def stop(self) -> None:
        self.started = False
        self._emit_connect(None)

    async def notify(self, data: bytes) -> int:
        self.sent.append(bytes(data))
        return 1 if self.is_connected else 0

    # 测试辅助
    def feed(self, data: bytes) -> None:
        """模拟"App 写入一帧"。"""
        self._emit_write(bytes(data))

    def feed_b0(self, **kw: Any) -> None:
        self.feed(P.build_b0(**kw))


# ══════════════════════════════════════════════════════════════
def _uuid16(short: int) -> Any:
    import uuid  # noqa: PLC0415

    return uuid.UUID(f"0000{short:04x}-0000-1000-8000-00805f9b34fb")


_BACKENDS: dict[str, type[PeripheralBackend]] = {
    Esp32Peripheral.kind: Esp32Peripheral,
    WinRtPeripheral.kind: WinRtPeripheral,
    LoopbackPeripheral.kind: LoopbackPeripheral,
}


def create_backend(kind: str = "esp32", **kwargs: Any) -> PeripheralBackend:
    """:param kind: ``"esp32"``（默认/现行）| ``"winrt"`` | ``"loopback"``"""
    try:
        cls = _BACKENDS[kind]
    except KeyError:
        raise PeripheralError(f"未知后端 {kind!r}，可选：{', '.join(sorted(_BACKENDS))}") from None
    return cls(**kwargs)
