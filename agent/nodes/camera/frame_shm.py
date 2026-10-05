"""本机帧总线：原始像素留在共享内存，Zenoh 消息里只放一句"指路牌"。

为什么要有这一层
----------------
需求 1d 的硬约束是 **绝不把原始帧 / JPEG / 视频流放进任何消息**。而 ``camera`` 节点
与 ``pose`` / ``face`` 节点是**不同进程**，摄像头画面必须传过去 —— 但一旦把像素塞进
Zenoh 的 payload（无论裸字节还是 JPEG 编码），就违背了这条约束。

所以这里把两件事彻底分开：

- **像素**：写进本机共享内存（``multiprocessing.shared_memory``），同机进程零拷贝可见，
  **永远不出本进程的内存**；
- **消息**：``feature/frame.raw`` 的 payload 只有一句指路牌
  （去哪个 block 读、多大、什么形状、第几帧），几十字节、纯 JSON。

于是"``feature/frame.raw`` 永不跨机"有两重保险：

1. 它本来就不在 ``topics.CROSS_MACHINE_ALLOWLIST`` 里（bridge 不放行）；
2. 即使真被转发，payload 里也**没有像素**，只有一句"去哪读"。

设计（seqlock + 环形块）
------------------------
- 每块 ``HEADER_SIZE`` 字节头部：前 8 字节是 **seqlock 计数**；像素在头部之后。
- 写者第 n 帧写第 ``n % SLOT_COUNT`` 块：写前把计数置**奇**（正在写），写完置**偶**（就绪）。
- 读者先读计数（奇 → 让一步重试），拷完像素再读一次，**两次一致**才算读到完整帧
  —— 这样即使写者正在覆盖同块，读者也不会拿到半张撕裂的图。
- 多块轮换让"写者连续刷帧、读者偶尔慢一拍"也能拿到干净数据。

块名带 pid（``<prefix>-<pid>-<i>``），避免上一次崩溃残留 / 另一个实例撞名。
"""

from __future__ import annotations

import os
import struct
import threading
import time
from typing import Any

try:  # numpy 是感知链路的既有关键依赖，缺失即无法工作 → 明确报错胜过后续 NameError
    import numpy as np
except ImportError:  # pragma: no cover - 环境问题
    np = None  # type: ignore[assignment]

try:
    from multiprocessing import shared_memory as _shm
except ImportError:  # pragma: no cover - 环境问题
    _shm = None  # type: ignore[assignment]

__all__ = [
    "FrameShmError",
    "FrameBuffer",
    "FrameReader",
    "DTYPE",
    "SLOT_COUNT",
    "HEADER_SIZE",
    "DEFAULT_NAME_PREFIX",
]

#: 像素 dtype（BGR 8bit）
DTYPE = "uint8"

#: 环形块数（写者轮换，读者几乎不会撞上"正在写"的那一块）
SLOT_COUNT = 3

#: 每块头部字节数（前 8 字节：seqlock 计数；其余预留）
HEADER_SIZE = 64

#: 单块容量上限（够 1080p BGR 一帧，再大就退化为"丢帧 + 告警"）
DEFAULT_CAPACITY = 1920 * 1080 * 3

#: 共享内存块名前缀（实际名会追加 pid 与块序号）
DEFAULT_NAME_PREFIX = "coyotelink-frame"

_HEADER_SEQ = struct.Struct("<Q")  # 前 8 字节：seqlock 计数


class FrameShmError(RuntimeError):
    """本机帧总线不可用（缺 numpy / 缺共享内存支持 / 容量不足）。"""


def _warn(logger: Any, msg: str, *args: Any) -> None:
    if logger is not None:
        logger.warning(msg, *args)


def _debug(logger: Any, msg: str, *args: Any) -> None:
    if logger is not None:
        logger.debug(msg, *args)


class FrameBuffer:
    """摄像头端：把一帧 BGR 写进本机共享内存，返回一句不含像素的指路牌（dict）。

    一个进程一份实例（camera 节点在 ``setup()`` 里建）。线程安全。
    """

    def __init__(
        self,
        *,
        slots: int = SLOT_COUNT,
        capacity: int | None = None,
        name_prefix: str = DEFAULT_NAME_PREFIX,
        logger: Any = None,
    ) -> None:
        if _shm is None or np is None:
            raise FrameShmError(
                "缺少 numpy 或 multiprocessing.shared_memory，本机帧总线不可用"
                "（摄像头画面无法在本机进程间传递）"
            )
        self._slots = max(2, int(slots))
        self._capacity = int(capacity or DEFAULT_CAPACITY)
        if self._capacity < 64:
            raise FrameShmError(f"块容量过小：{self._capacity}（至少要放下一帧）")
        self._prefix = f"{name_prefix}-{os.getpid()}"
        self.log = logger
        self._lock = threading.Lock()
        self._ready_count = [0] * self._slots
        self._blocks: list[Any] = []
        self._names: list[str] = []
        self._closed = False
        for index in range(self._slots):
            name = f"{self._prefix}-{index}"
            block = _shm.SharedMemory(name=name, create=True, size=HEADER_SIZE + self._capacity)
            block.buf[0:8] = b"\x00" * 8  # 计数归零 = 尚无有效帧
            self._blocks.append(block)
            self._names.append(name)
        _debug(self.log, "本机帧总线就绪：%d 块 × %d 字节（前缀 %s）", self._slots, self._capacity, self._prefix)

    @property
    def names(self) -> list[str]:
        return list(self._names)

    def write(
        self,
        frame: Any,
        *,
        ts_ms: int | None = None,
        seq: int = 0,
        epoch: int = 0,
    ) -> dict[str, Any] | None:
        """写入一帧，返回指路牌 dict；帧过大 / 已关闭时返回 ``None``（不抛）。"""
        if self._closed:
            return None
        if frame is None:
            return None
        try:
            arr = np.ascontiguousarray(frame, dtype=np.uint8)
        except (TypeError, ValueError) as exc:
            _warn(self.log, "帧无法转成 uint8 数组（%s），丢弃", exc)
            return None
        nbytes = int(arr.nbytes)
        if nbytes <= 0 or nbytes > self._capacity:
            _warn(self.log, "帧 %d 字节超出块容量 %d，丢弃", nbytes, self._capacity)
            return None
        shape = [int(x) for x in arr.shape]
        payload = arr.tobytes()

        with self._lock:
            index = int(seq) % self._slots
            block = self._blocks[index]
            before = self._ready_count[index]
            # seqlock：写前奇数（读者见到奇数即让路）
            block.buf[0:8] = _HEADER_SEQ.pack(before * 2 + 1)
            block.buf[HEADER_SIZE : HEADER_SIZE + nbytes] = payload
            # 写后偶数（+1 = 新的一帧就绪）
            self._ready_count[index] = before + 1
            block.buf[0:8] = _HEADER_SEQ.pack((before + 1) * 2)
            name = self._names[index]

        return {
            "ts": int(ts_ms if ts_ms is not None else time.time() * 1000),
            "seq": int(seq),
            "epoch": int(epoch),
            "shm": name,
            "shape": shape,
            "dtype": DTYPE,
            "nbytes": nbytes,
            "slot": index,
        }

    def close(self) -> None:
        """释放全部块（幂等）。只有整块都没人用时才真正回收（Windows 引用计数）。"""
        with self._lock:
            if self._closed:
                self._blocks.clear()
                return
            self._closed = True
            blocks, self._blocks = self._blocks, []
            self._names = []
        for block in blocks:
            try:
                block.close()
            except Exception as exc:  # noqa: BLE001
                _warn(self.log, "关闭帧块失败：%s", exc)
            try:
                block.unlink()
            except Exception as exc:  # noqa: BLE001 - Windows 上重复 unlink 会抛，忽略
                _debug(self.log, "unlink 帧块失败（可忽略）：%s", exc)
        _debug(self.log, "本机帧总线已关闭")


class FrameReader:
    """消费端：按指路牌把像素读回 ``numpy.ndarray``（BGR）。

    一个进程可建多份（pose / face 各一份），也可共享一份；线程安全。
    块被写者释放后 attach 会失败 → 返回 ``None``（下次指路牌换了新名会自动重连）。
    """

    def __init__(self, *, logger: Any = None) -> None:
        if _shm is None or np is None:
            raise FrameShmError("缺少 numpy 或 multiprocessing.shared_memory，本机帧总线不可用")
        self.log = logger
        self._lock = threading.Lock()
        self._blocks: dict[str, Any] = {}
        self._closed = False

    def _attach(self, name: str) -> Any:
        block = self._blocks.get(name)
        if block is not None:
            return block
        block = _shm.SharedMemory(name=name)
        self._blocks[name] = block
        return block

    def read(self, descriptor: Any) -> Any:
        """返回 BGR ``ndarray`` 或 ``None``（指路牌无效 / 块未就绪 / 读到撕裂）。"""
        if self._closed or not isinstance(descriptor, dict):
            return None
        name = descriptor.get("shm")
        shape = descriptor.get("shape")
        try:
            nbytes = int(descriptor.get("nbytes") or 0)
        except (TypeError, ValueError):
            return None
        if not name or not shape or nbytes <= 0:
            return None

        with self._lock:
            try:
                block = self._attach(name)
            except FileNotFoundError:
                return None
            if block is None or len(block.buf) < HEADER_SIZE + nbytes:
                return None
            for _ in range(4):
                head1 = _HEADER_SEQ.unpack_from(block.buf, 0)[0]
                if head1 & 1:  # 写者正在写
                    time.sleep(0.001)
                    continue
                raw = bytes(block.buf[HEADER_SIZE : HEADER_SIZE + nbytes])
                head2 = _HEADER_SEQ.unpack_from(block.buf, 0)[0]
                if head1 != head2:  # 拷贝期间被覆盖 → 撕裂
                    continue
                try:
                    array = np.frombuffer(raw, dtype=DTYPE).reshape(tuple(int(x) for x in shape))
                except (ValueError, TypeError):
                    return None
                return array.copy()
            _debug(self.log, "帧 %s 多次检出撕裂，本帧放弃", name)
            return None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            blocks, self._blocks = self._blocks, {}
        for block in blocks.values():
            try:
                block.close()
            except Exception as exc:  # noqa: BLE001
                _debug(self.log, "关闭帧块（读端）失败：%s", exc)
