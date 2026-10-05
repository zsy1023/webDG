"""ESP32 ⇄ 主机 的**串口链路细节**（T1 收尾）—— 只做"打开 / 收发字节 / 关闭"。

分层（别乱放）：

* 本模块：串口物理链路的开关与字节收发 + **列出本机可用串口的 helper**（web 侧拉下拉要用）；
* :mod:`agent.nodes.sim_coyote.transport` 的 ``Esp32Peripheral``：把字节流接进协议层、断线重连编排；
* :mod:`agent.nodes.sim_coyote.protocol`：B0/BF/B1 的语义 —— 本模块**完全不认识**它们。

本机实测的两个坑（都踩过，写在代码里防复发）：

1. **打开串口会把板子复位**：CH340/CP210x 这类板子的 DTR/RTS 接了自动复位电路，
   pyserial 打开时默认会把它们拉起来 → 板子复位、甚至进下载模式。
   所以 :func:`open_serial` 在 **open 之前**就把 ``dtr``/``rts`` 置 False，打开后再压一次。
2. **蓝牙虚拟串口能"打开成功"却收不到任何字节**（本机 COM5/COM6 就是）。
   :func:`list_serial_ports` 把它们标成 ``is_bluetooth``，:func:`pick_likely_esp32` 会跳过。
   "打开成功"不代表链路可用 —— 真正的判据是**有没有假主机的应答**（见 ``Esp32Peripheral.start`` 的在线探测）。

命令行自检（列本机串口）::

    python -m agent.nodes.sim_coyote.esp32_link
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

__all__ = [
    "DEFAULT_BAUDRATE",
    "DOWNLINK_HEARTBEAT",
    "HEARTBEAT",
    "LOGLINE_PREFIX",
    "SerialPortInfo",
    "LinkError",
    "ByteStream",
    "SerialLink",
    "open_serial",
    "list_serial_ports",
    "pick_likely_esp32",
]

log = logging.getLogger("coyotelink.sim_coyote.esp32_link")

#: 串口默认波特率（与 transport.py 契约、固件 platformio.ini 一致）
DEFAULT_BAUDRATE = 115200

#: 下行心跳（主机 → ESP32）：固件收到会回一条 ``#`` 日志行，用于在线探测
DOWNLINK_HEARTBEAT = 0xCE
HEARTBEAT = DOWNLINK_HEARTBEAT  # 别名，方便阅读

#: 固件日志行前缀（**统一 '#' + 换行**）：PC 侧按行整行丢弃，不污染二进制帧流
LOGLINE_PREFIX = ord("#")

#: USB-TTL 桥接芯片（用来判断"这个口像不像 ESP32 开发板"）
_USB_BRIDGE_VIDS: dict[int, str] = {
    0x1A86: "WCH CH34x",  # 本机板子就是它
    0x10C4: "Silicon Labs CP210x",
    0x0403: "FTDI",
    0x067B: "Prolific PL2303",
}

#: 蓝牙虚拟串口的特征（hwid 前缀或描述里出现）
_BLUETOOTH_MARKERS: tuple[str, ...] = ("bthenum", "bluetooth", "蓝牙", "標準串行", "标准串行")


class LinkError(RuntimeError):
    """串口链路打开 / 读写失败。"""


# ══════════════════════════════════════════════════════════════
# 列出本机串口（web 侧下拉的**唯一数据来源**）
# ══════════════════════════════════════════════════════════════
@dataclass
class SerialPortInfo:
    """一个可用串口。``id`` 就是设备名（``COM18``）—— web 侧把这个原样发回 select_device。"""

    device: str
    description: str = ""
    hwid: str = ""
    vid: int | None = None
    pid: int | None = None
    serial_number: str = ""
    #: 蓝牙虚拟串口：**能打开但收不到字节**，别选它
    is_bluetooth: bool = False
    #: 看起来像 USB-TTL 桥接的板子（CH340/CP210x/FTDI…）
    likely_esp32: bool = False

    @property
    def id(self) -> str:
        return self.device

    @property
    def label(self) -> str:
        """给前端显示用的一行字（例如 ``COM18 — USB-SERIAL CH340``）。"""
        desc = self.description or self.hwid or "未知设备"
        return f"{self.device} — {desc}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "device": self.device,
            "name": self.label,
            "description": self.description,
            "hwid": self.hwid,
            "vid": self.vid,
            "pid": self.pid,
            "serial_number": self.serial_number,
            "is_bluetooth": self.is_bluetooth,
            "likely_esp32": self.likely_esp32,
        }


def list_serial_ports() -> list[SerialPortInfo]:
    """列出本机串口。蓝牙虚拟口排在最后并带标记（别选它）。

    没有 pyserial 时返回空列表（**不抛异常** —— 列表拿不到不该让节点崩）。
    """
    try:
        from serial.tools import list_ports  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        log.warning("pyserial 不可用，无法枚举串口：%s", exc)
        return []

    out: list[SerialPortInfo] = []
    try:
        comports = list(list_ports.comports())
    except Exception as exc:  # noqa: BLE001
        log.warning("枚举串口失败：%s", exc)
        return []

    for port in comports:
        description = str(getattr(port, "description", "") or "")
        hwid = str(getattr(port, "hwid", "") or "")
        haystack = f"{hwid} {description}".lower()
        is_bluetooth = any(marker in haystack for marker in _BLUETOOTH_MARKERS)
        vid = getattr(port, "vid", None)
        pid = getattr(port, "pid", None)
        out.append(
            SerialPortInfo(
                device=str(port.device),
                description=description,
                hwid=hwid,
                vid=vid,
                pid=pid,
                serial_number=str(getattr(port, "serial_number", "") or ""),
                is_bluetooth=is_bluetooth,
                likely_esp32=(not is_bluetooth and vid in _USB_BRIDGE_VIDS),
            )
        )
    # 排口顺序：能用的在前，蓝牙虚拟口垫底，同组按端口名
    return sorted(out, key=lambda p: (p.is_bluetooth, p.device))


def pick_likely_esp32(ports: list[SerialPortInfo] | None = None) -> str | None:
    """挑一个"最像假主机"的串口（跳过蓝牙虚拟口）。挑不到返回 ``None``。"""
    for port in ports if ports is not None else list_serial_ports():
        if port.likely_esp32:
            return port.device
    return None


# ══════════════════════════════════════════════════════════════
# 字节流抽象（单测注入假实现就能脱硬件跑）
# ══════════════════════════════════════════════════════════════
class ByteStream(Protocol):
    """``pyserial.Serial`` 的**最小子集** —— 便于单测注入假的 stream。

    ``in_waiting`` 是可选优化：读线程用它把"已经就绪的字节"一次取走
    （没有它也能跑，只是每轮多读一次）。
    """

    def read(self, size: int = 1) -> bytes:  # pragma: no cover - 协议
        ...

    @property
    def in_waiting(self) -> int:  # pragma: no cover - 协议（可选）
        ...

    def write(self, data: bytes) -> int:  # pragma: no cover - 协议
        ...

    def close(self) -> None:  # pragma: no cover - 协议
        ...


def open_serial(
    port: str,
    baudrate: int = DEFAULT_BAUDRATE,
    *,
    read_timeout: float = 0.05,
    write_timeout: float = 0.5,
) -> ByteStream:
    """打开串口，并**压住 DTR/RTS**（否则板子会被复位/拖进下载模式）。

    :raises LinkError: 端口不存在 / 被占用 / 打开失败
    """
    import serial  # noqa: PLC0415

    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baudrate
    ser.timeout = read_timeout  # 短超时：读线程靠它周期醒来看停止标志
    ser.write_timeout = write_timeout
    ser.dsrdtr = False  # 不用 DTR/DSR 流控
    ser.rtscts = False  # 不用 RTS/CTS 流控
    if not port:
        raise LinkError("串口名称为空 —— 请在 agent/config.toml 的 [sim_coyote] port 里填板子所在的串口")
    # ★ 关键：**open 之前**就把 DTR/RTS 压成 False
    try:
        ser.dtr = False
        ser.rts = False
    except Exception:  # noqa: BLE001 - 某些驱动/端口不支持，忽略
        pass
    try:
        ser.open()
    except Exception as exc:  # noqa: BLE001
        raise LinkError(f"打开串口 {port} 失败：{exc}") from exc
    try:
        ser.dtr = False
        ser.rts = False
        ser.reset_input_buffer()  # 丢掉打开前排队的陈旧字节
    except Exception:  # noqa: BLE001
        pass
    return ser


# ══════════════════════════════════════════════════════════════
# 串口链路：后台线程收字节 → 转交事件循环
# ══════════════════════════════════════════════════════════════
class SerialLink:
    """一条串口链路（**不管重连** —— 那是 :class:`Esp32Peripheral` 的编排）。

    :param on_bytes: 收到字节时回调（**在事件循环线程上**执行）
    :param on_closed: 链路意外断开时回调 ``on_closed(exc)``（事件循环线程上）
    :param open_fn: 打开函数（默认 :func:`open_serial`；单测注入假实现）
    """

    def __init__(
        self,
        port: str,
        baudrate: int = DEFAULT_BAUDRATE,
        *,
        loop: asyncio.AbstractEventLoop,
        on_bytes: Callable[[bytes], None],
        on_closed: Callable[[BaseException], None] | None = None,
        open_fn: Callable[..., ByteStream] | None = None,
        read_chunk: int = 256,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self._loop = loop
        self._on_bytes = on_bytes
        self._on_closed = on_closed
        self._open_fn = open_fn or open_serial  # 运行时解析，便于单测注入假 stream
        self._read_chunk = read_chunk

        self._stream: ByteStream | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self._closed_notified = False
        #: 读写已经失败过（链路已坏，但还没 close）
        self._broken = False

    # ── 生命周期 ──────────────────────────────────────────────
    @property
    def is_open(self) -> bool:
        """链路是否可用（**读写失败过也算不可用**，不必等 close）。"""
        return self._stream is not None and not self._broken

    def open(self) -> None:
        """打开串口并启动收字节线程。**同步**，失败抛 :class:`LinkError`。"""
        if self._stream is not None:
            return
        self._stop.clear()
        self._closed_notified = False
        self._broken = False
        stream = self._open_fn(self.port, self.baudrate)
        self._stream = stream
        self._thread = threading.Thread(
            target=self._reader_loop, name=f"esp32-link-{self.port}", daemon=True
        )
        self._thread.start()
        log.info("串口链路已打开：%s @ %d 8N1", self.port, self.baudrate)

    def close(self) -> None:
        """停线程 + 关串口（幂等）。"""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    # ── 收发 ──────────────────────────────────────────────────
    def write(self, data: bytes) -> int:
        """写字节（下行：B1 / 心跳）。链路没开返回 0。"""
        stream = self._stream
        if stream is None:
            return 0
        try:
            with self._write_lock:
                written = stream.write(bytes(data))
            return int(written or 0)
        except Exception as exc:  # noqa: BLE001
            log.warning("串口写失败：%s", exc)
            self._notify_closed(exc)
            return 0

    def send_heartbeat(self) -> int:
        """发一次下行心跳（``0xCE``）—— 固件会回一条 ``#`` 日志行，用于在线探测。"""
        return self.write(bytes([DOWNLINK_HEARTBEAT]))

    # ── 内部 ──────────────────────────────────────────────────
    def _reader_loop(self) -> None:
        stream = self._stream
        while not self._stop.is_set() and stream is not None:
            try:
                # ★ 读法是有讲究的（本机实测，T1 排障结论）：
                #   先 read(1) 只等"第一个字节"（有数据立刻返回，实测 ~0.2ms），
                #   再把驱动缓冲里**已经就绪**的其余字节一次取走。
                #   ❌ 千万别直接 read(256) / read(4096)：Windows 串口驱动会等到
                #      "凑满请求长度或读超时"才返回 —— 实测把一次往返拖到 ~88ms，
                #      App 上就是"调强度反应慢"。
                chunk = stream.read(1)
                if not chunk:
                    continue  # 读超时（timeout=0.05）→ 回到循环顶部看停止标志
                waiting = int(getattr(stream, "in_waiting", 0) or 0)
                if waiting > 0:
                    more = stream.read(min(waiting, self._read_chunk))
                    if more:
                        chunk += more
            except Exception as exc:  # noqa: BLE001 - 拔线 / 端口被抢占
                if not self._stop.is_set():
                    log.warning("串口读失败（链路视为断开）：%s", exc)
                    self._notify_closed(exc)
                return
            self._dispatch(chunk)

    def _dispatch(self, chunk: bytes) -> None:
        """把字节转交事件循环线程（读取线程里**不做**任何业务处理）。"""
        try:
            self._loop.call_soon_threadsafe(self._deliver, bytes(chunk))
        except RuntimeError:  # 事件循环已关闭
            self._stop.set()

    def _deliver(self, chunk: bytes) -> None:
        try:
            self._on_bytes(chunk)
        except Exception:  # noqa: BLE001 - 回调出错不能把链路打崩
            log.exception("处理串口字节时出错")

    def _notify_closed(self, exc: BaseException) -> None:
        if self._closed_notified:
            return
        self._closed_notified = True
        self._broken = True  # is_open 立刻变 False（别再往坏链路上写）
        self._stop.set()
        if self._on_closed is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._on_closed, exc)
        except RuntimeError:
            pass


# ══════════════════════════════════════════════════════════════
# 命令行自检：列出本机串口
# ══════════════════════════════════════════════════════════════
def _main() -> int:  # pragma: no cover - 手工排查用
    ports = list_serial_ports()
    if not ports:
        print("没有找到任何串口（pyserial 装了吗？板子插了吗？）")
        return 1
    print(f"本机 {len(ports)} 个串口：")
    for port in ports:
        marks = []
        if port.likely_esp32:
            marks.append("★ 像 ESP32 开发板")
        if port.is_bluetooth:
            marks.append("蓝牙虚拟口（能打开但收不到字节，别选）")
        print(f"  {port.label}   [vid={port.vid} pid={port.pid}] {' '.join(marks)}")
    guess = pick_likely_esp32(ports)
    if guess:
        print(f"\n建议填进 agent/config.toml：[sim_coyote] port = \"{guess}\"")
    return 0


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(_main())
