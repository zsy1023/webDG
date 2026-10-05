"""郊狼主机 3.0 的 BLE V3 协议编解码 —— **纯逻辑，不碰蓝牙、不碰网络**。

权威依据（冲突时以真机为准）：

* 官方逐字节规格：``reference/dglab-bluetooth-protocol/coyote/v3/README.md``
* 官方报文实例：``reference/dglab-bluetooth-protocol/coyote/v3/example.md``
* **真机实测**：``docs/research/郊狼真机GATT实测.md`` §5 / §6（2026-10-03，bleak 直连）

本模块只做「``bytes`` ⇄ 结构化」与「主机状态推进」，因此可以脱离硬件单测
（T1 任务书 §8 的硬性要求）。

方向约定：

* **App → 主机**：``B0``（20 字节）、``BF``（7 字节）
* **主机 → App**：``B1``（4 字节）

真机实测修正（见实测文档 §5.2）：
    协议文档只说「强度发生变化时才回 ``B1``」，但真机上**只要 ``B0`` 的序列号 ≠ 0，
    就必定回一条相同序列号的 ``B1``**，哪怕强度一个字节没变。
    故 :meth:`B0Command.expects_reply` 依据「序列号 ≠ 0」而不是「强度是否变化」。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Union

__all__ = [
    "ProtocolError",
    "StrengthMode",
    "WaveChannel",
    "B0Command",
    "BfCommand",
    "Command",
    "CoyoteState",
    "HEAD_B0",
    "HEAD_BF",
    "HEAD_B1",
    "B0_LEN",
    "BF_LEN",
    "B1_LEN",
    "STRENGTH_MAX",
    "FREQ_MIN",
    "FREQ_MAX",
    "WAVE_AMP_MAX",
    "DEVICE_NAME_COYOTE",
    "DEVICE_NAME_SENSOR",
    "clamp_strength",
    "parse_b0",
    "build_b0",
    "parse_bf",
    "build_bf",
    "build_b1",
    "parse_b1",
    "parse_command",
]

# ──────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────
HEAD_B0 = 0xB0
HEAD_BF = 0xBF
HEAD_B1 = 0xB1

B0_LEN = 20
BF_LEN = 7
B1_LEN = 4

STRENGTH_MAX = 200  # 通道强度 0~200，超范围按 0 处理
FREQ_MIN = 10  # 波形频率 10~240（App 侧先按 10~1000 换算过来）
FREQ_MAX = 240
WAVE_AMP_MAX = 100  # 波形强度 0~100；>100 → 该通道 4 组整组作废

#: 真机广播名（实测：设备名就是它，App 靠它识别）
DEVICE_NAME_COYOTE = "47L121000"  # 脉冲主机 3.0
DEVICE_NAME_SENSOR = "47L120100"  # 无线传感器 3.0


class ProtocolError(ValueError):
    """报文长度 / HEAD / 取值非法。"""


class StrengthMode(IntEnum):
    """强度值解读方式（``B0`` byte1 的低 4 bit：高 2 bit = A 通道，低 2 bit = B 通道）。"""

    NONE = 0b00  # 该通道强度不变（设定值无效）
    ADD = 0b01  # 相对增加
    SUB = 0b10  # 相对减少
    SET = 0b11  # 绝对设置


def clamp_strength(value: int) -> int:
    """把强度夹到 0~200。"""
    return 0 if value < 0 else STRENGTH_MAX if value > STRENGTH_MAX else value


# ──────────────────────────────────────────────────────────────
# 波形：每通道 4 组「频率 + 强度」，每组代表 25ms，4 组 = 100ms
# ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class WaveChannel:
    """一个通道的 4 组波形数据。

    :param freq: 4 个波形频率
    :param amp: 4 个波形强度
    :param valid: ``False`` 表示该通道整组作废（真机不输出该通道波形）。
        真机规则：**只要有一个取值越界（freq 不在 10~240，或 amp > 100），
        整组 4 条一起丢弃**。塞一个 ``amp=101`` 就是「本通道不输出」的开关。
    """

    freq: tuple[int, int, int, int]
    amp: tuple[int, int, int, int]
    valid: bool

    @staticmethod
    def build(freq: Any, amp: Any) -> "WaveChannel":
        f = tuple(int(x) for x in freq)
        a = tuple(int(x) for x in amp)
        if len(f) != 4 or len(a) != 4:
            raise ProtocolError(f"每通道必须 4 组频率/强度，得到 freq={f!r} amp={a!r}")
        valid = all(FREQ_MIN <= x <= FREQ_MAX for x in f) and all(0 <= x <= WAVE_AMP_MAX for x in a)
        return WaveChannel(freq=f, amp=a, valid=valid)  # type: ignore[arg-type]

    @property
    def silent(self) -> bool:
        """该通道是否一定不输出（作废，或 4 组强度全 0）。"""
        return (not self.valid) or all(x == 0 for x in self.amp)

    def to_dict(self) -> dict[str, Any]:
        return {
            "freq": list(self.freq),
            "amp": list(self.amp),
            "valid": self.valid,
            "silent": self.silent,
        }


# ──────────────────────────────────────────────────────────────
# B0：App → 主机（每 100ms 一条，固定 20 字节）
# ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class B0Command:
    """``B0`` 指令 —— 一次写入两通道的强度变化 + 波形数据。

    布局（20 字节）::

        0    1       0xB0
        1    1       序列号(高 4bit) | 强度值解读方式(低 4bit)
        2    1       A 通道强度设定值 (0~200)
        3    1       B 通道强度设定值 (0~200)
        4    4       A 通道波形频率 ×4
        8    4       A 通道波形强度 ×4
        12   4       B 通道波形频率 ×4
        16   4       B 通道波形强度 ×4

    v3 协议**不做大小端转换**。
    """

    seq: int
    mode_a: StrengthMode
    mode_b: StrengthMode
    strength_a: int
    strength_b: int
    wave_a: WaveChannel
    wave_b: WaveChannel
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not 0 <= self.seq <= 15:
            raise ProtocolError(f"序列号必须在 0~15，得到 {self.seq}")
        if not 0 <= self.strength_a <= 255 or not 0 <= self.strength_b <= 255:
            raise ProtocolError("强度设定值必须是 1 字节（0~255），越界值在 apply 时按 0 处理")

    @property
    def expects_reply(self) -> bool:
        """要不要回 ``B1``？真机实测：**序列号 ≠ 0 就回**。"""
        return self.seq != 0

    def strength_delta(self) -> tuple[int | None, int | None]:
        """给上层看的「强度意图」：``None`` = 不变，``+n``/``-n`` = 增减，整数 = 绝对值。"""
        return (self._delta(self.mode_a, self.strength_a), self._delta(self.mode_b, self.strength_b))

    @staticmethod
    def _delta(mode: StrengthMode, value: int) -> int | None:
        if mode is StrengthMode.NONE:
            return None
        v = 0 if value > STRENGTH_MAX else value  # 越界值按 0 处理
        if mode is StrengthMode.ADD:
            return v
        if mode is StrengthMode.SUB:
            return -v
        return v  # SET

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "b0",
            "ts": self.ts,
            "seq": self.seq,
            "mode": {"A": int(self.mode_a), "B": int(self.mode_b)},
            "mode_name": {"A": self.mode_a.name, "B": self.mode_b.name},
            "strength": {"A": self.strength_a, "B": self.strength_b},
            "wave_a": self.wave_a.to_dict(),
            "wave_b": self.wave_b.to_dict(),
        }


def parse_b0(data: bytes | bytearray | memoryview) -> B0Command:
    """20 字节 ``B0`` → :class:`B0Command`。"""
    b = bytes(data)
    if len(b) != B0_LEN:
        raise ProtocolError(f"B0 必须是 {B0_LEN} 字节，得到 {len(b)}")
    if b[0] != HEAD_B0:
        raise ProtocolError(f"不是 B0 指令（HEAD=0x{b[0]:02X}）")
    seq = (b[1] >> 4) & 0x0F
    mode_a = StrengthMode((b[1] >> 2) & 0b11)
    mode_b = StrengthMode(b[1] & 0b11)
    return B0Command(
        seq=seq,
        mode_a=mode_a,
        mode_b=mode_b,
        strength_a=b[2],
        strength_b=b[3],
        wave_a=WaveChannel.build(b[4:8], b[8:12]),
        wave_b=WaveChannel.build(b[12:16], b[16:20]),
    )


def build_b0(
    seq: int,
    mode_a: StrengthMode | int,
    mode_b: StrengthMode | int,
    strength_a: int,
    strength_b: int,
    wave_a: WaveChannel | tuple[Any, Any],
    wave_b: WaveChannel | tuple[Any, Any],
) -> bytes:
    """结构化 → 20 字节 ``B0``（单测与模拟主机回放用）。"""
    cmd = B0Command(
        seq=seq,
        mode_a=StrengthMode(mode_a),
        mode_b=StrengthMode(mode_b),
        strength_a=strength_a,
        strength_b=strength_b,
        wave_a=wave_a if isinstance(wave_a, WaveChannel) else WaveChannel.build(*wave_a),
        wave_b=wave_b if isinstance(wave_b, WaveChannel) else WaveChannel.build(*wave_b),
    )
    return _pack_b0(cmd)


def _pack_b0(c: B0Command) -> bytes:
    byte1 = ((c.seq & 0x0F) << 4) | ((int(c.mode_a) & 0b11) << 2) | (int(c.mode_b) & 0b11)
    out = bytearray(B0_LEN)
    out[0] = HEAD_B0
    out[1] = byte1
    out[2] = c.strength_a & 0xFF
    out[3] = c.strength_b & 0xFF
    out[4:8] = bytes(x & 0xFF for x in c.wave_a.freq)
    out[8:12] = bytes(x & 0xFF for x in c.wave_a.amp)
    out[12:16] = bytes(x & 0xFF for x in c.wave_b.freq)
    out[16:20] = bytes(x & 0xFF for x in c.wave_b.amp)
    return bytes(out)


# ──────────────────────────────────────────────────────────────
# BF：App → 主机（固定 7 字节，**无回包、断电保存**）
# ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class BfCommand:
    """``BF`` 指令 —— 软上限 + 频率/强度平衡参数。

    布局（7 字节）::

        0    1       0xBF
        1    1       A 通道强度软上限 (0~200，越界不修改)
        2    1       B 通道强度软上限 (0~200，越界不修改)
        3    1       A 通道波形频率平衡参数 (0~255)
        4    1       B 通道波形频率平衡参数 (0~255)
        5    1       A 通道波形强度平衡参数 (0~255)
        6    1       B 通道波形强度平衡参数 (0~255)

    ⚠️ 真机行为：写入后**直接生效、没有返回值**，且**断电保存**；
    每次重连设备后 App 都会重写一次，所以模拟主机在「新连接建立」时
    应把软上限重置为持久化值（不要沿用上次连接的内存状态）。
    """

    soft_cap_a: int
    soft_cap_b: int
    freq_balance_a: int
    freq_balance_b: int
    amp_balance_a: int
    amp_balance_b: int
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "bf",
            "ts": self.ts,
            "soft_cap": {"A": self.soft_cap_a, "B": self.soft_cap_b},
            "freq_balance": {"A": self.freq_balance_a, "B": self.freq_balance_b},
            "amp_balance": {"A": self.amp_balance_a, "B": self.amp_balance_b},
        }


def parse_bf(data: bytes | bytearray | memoryview) -> BfCommand:
    b = bytes(data)
    if len(b) != BF_LEN:
        raise ProtocolError(f"BF 必须是 {BF_LEN} 字节，得到 {len(b)}")
    if b[0] != HEAD_BF:
        raise ProtocolError(f"不是 BF 指令（HEAD=0x{b[0]:02X}）")
    return BfCommand(*b[1:7])


def build_bf(
    soft_cap_a: int,
    soft_cap_b: int,
    freq_balance_a: int = 0,
    freq_balance_b: int = 0,
    amp_balance_a: int = 0,
    amp_balance_b: int = 0,
) -> bytes:
    return bytes([HEAD_BF, soft_cap_a & 0xFF, soft_cap_b & 0xFF,
                  freq_balance_a & 0xFF, freq_balance_b & 0xFF,
                  amp_balance_a & 0xFF, amp_balance_b & 0xFF])


# ──────────────────────────────────────────────────────────────
# B1：主机 → App（4 字节）
# ──────────────────────────────────────────────────────────────
def build_b1(seq: int, strength_a: int, strength_b: int) -> bytes:
    """``B1 | 序列号(1B) | A 实际强度 | B 实际强度``（真机实测格式）。"""
    if not 0 <= seq <= 255:
        raise ProtocolError(f"B1 序列号必须是 1 字节，得到 {seq}")
    return bytes([HEAD_B1, seq & 0xFF, clamp_strength(strength_a), clamp_strength(strength_b)])


def parse_b1(data: bytes | bytearray | memoryview) -> tuple[int, int, int]:
    """``B1`` → ``(序列号, A 实际强度, B 实际强度)``。"""
    b = bytes(data)
    if len(b) != B1_LEN:
        raise ProtocolError(f"B1 必须是 {B1_LEN} 字节，得到 {len(b)}")
    if b[0] != HEAD_B1:
        raise ProtocolError(f"不是 B1 指令（HEAD=0x{b[0]:02X}）")
    return b[1], b[2], b[3]


# ──────────────────────────────────────────────────────────────
# 分发
# ──────────────────────────────────────────────────────────────
Command = Union[B0Command, BfCommand]


def parse_command(data: bytes | bytearray | memoryview) -> Command:
    """按 HEAD 分发解析 App → 主机的指令。"""
    b = bytes(data)
    if not b:
        raise ProtocolError("空报文")
    if b[0] == HEAD_B0:
        return parse_b0(b)
    if b[0] == HEAD_BF:
        return parse_bf(b)
    raise ProtocolError(f"未知指令 HEAD=0x{b[0]:02X}（长度 {len(b)}）")


# ──────────────────────────────────────────────────────────────
# 模拟主机的设备状态
# ──────────────────────────────────────────────────────────────
@dataclass
class CoyoteState:
    """一台「郊狼主机」的内部状态。

    模拟硬件必须维护它：``B0`` 的 ADD/SUB 是**相对**的，不知道当前强度就算不出新强度，
    也就回不出正确的 ``B1``。

    :param soft_cap_a: A 通道强度软上限（``BF`` 设定，断电保存）
    :param soft_cap_b: B 通道强度软上限
    """

    strength_a: int = 0
    strength_b: int = 0
    soft_cap_a: int = STRENGTH_MAX
    soft_cap_b: int = STRENGTH_MAX
    freq_balance_a: int = 0
    freq_balance_b: int = 0
    amp_balance_a: int = 0
    amp_balance_b: int = 0
    wave_a: WaveChannel = field(default_factory=lambda: WaveChannel((0, 0, 0, 0), (0, 0, 0, 0), False))
    wave_b: WaveChannel = field(default_factory=lambda: WaveChannel((0, 0, 0, 0), (0, 0, 0, 0), False))

    # ── 应用指令 ────────────────────────────────────────────
    def apply(self, cmd: Command) -> bool:
        """应用一条指令，返回**通道强度是否发生变化**。

        :class:`BfCommand` 只改软上限/平衡参数（强度绝对值不会被它改写，
        但缩减软上限会立刻把超出的强度压回上限 —— 与真机一致）。
        """
        if isinstance(cmd, BfCommand):
            return self._apply_bf(cmd)
        return self._apply_b0(cmd)

    def _apply_b0(self, cmd: B0Command) -> bool:
        before = (self.strength_a, self.strength_b)
        self.strength_a = self._step(
            self.strength_a, cmd.mode_a, cmd.strength_a, self.soft_cap_a
        )
        self.strength_b = self._step(
            self.strength_b, cmd.mode_b, cmd.strength_b, self.soft_cap_b
        )
        if cmd.wave_a.valid:
            self.wave_a = cmd.wave_a
        if cmd.wave_b.valid:
            self.wave_b = cmd.wave_b
        return (self.strength_a, self.strength_b) != before

    def _apply_bf(self, cmd: BfCommand) -> bool:
        before = (self.strength_a, self.strength_b)
        if cmd.soft_cap_a <= STRENGTH_MAX:
            self.soft_cap_a = cmd.soft_cap_a
        if cmd.soft_cap_b <= STRENGTH_MAX:
            self.soft_cap_b = cmd.soft_cap_b
        self.freq_balance_a = cmd.freq_balance_a
        self.freq_balance_b = cmd.freq_balance_b
        self.amp_balance_a = cmd.amp_balance_a
        self.amp_balance_b = cmd.amp_balance_b
        # 软上限收紧后，当前强度要立刻压回上限
        self.strength_a = min(self.strength_a, self.soft_cap_a)
        self.strength_b = min(self.strength_b, self.soft_cap_b)
        return (self.strength_a, self.strength_b) != before

    @staticmethod
    def _step(current: int, mode: StrengthMode, value: int, soft_cap: int) -> int:
        if mode is StrengthMode.NONE:
            return current
        v = 0 if value > STRENGTH_MAX else value  # 文档 & 真机：越界设定值按 0
        if mode is StrengthMode.ADD:
            new = current + v
        elif mode is StrengthMode.SUB:
            new = current - v
        else:  # SET
            new = v
        return max(0, min(new, soft_cap))

    # ── 回包 ────────────────────────────────────────────────
    def b1_for(self, cmd: B0Command) -> bytes | None:
        """要不要为这条 ``B0`` 回 ``B1``？要就给出报文。

        真机实测：**序列号 ≠ 0 就回**（不看强度是否真的变了）。
        """
        if not cmd.expects_reply:
            return None
        return build_b1(cmd.seq, self.strength_a, self.strength_b)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strength": {"A": self.strength_a, "B": self.strength_b},
            "soft_cap": {"A": self.soft_cap_a, "B": self.soft_cap_b},
            "freq_balance": {"A": self.freq_balance_a, "B": self.freq_balance_b},
            "amp_balance": {"A": self.amp_balance_a, "B": self.amp_balance_b},
        }
