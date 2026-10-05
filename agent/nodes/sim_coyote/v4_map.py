"""B0/BF → V4 语义映射（T9「桥接」的**解析**这一半）。

关键事实（2026-10-04 从官方 SDK 实测抓到的 `device.op` 帧）::

    {'m': 'device.op', 'data': {'s': 'slot-sim-1', 'c': 0, 'p': 1, 'im': False,
                                't': V4ActionType.AppendPulseData, 'd': 800,
                                'v': ['0A0A0A0A00000000', '0A0A0A0A64646464', ...], 'ver': 3}}

`v` 里每个元素就是 **`波形频率×4 + 波形强度×4` 的 8 字节 hex** —— 与 B0 里
**一个通道那 8 个字节完全对应**。所以 B0 → V4 **不需要波形名查表**：

    B0（每 100ms，两通道各 8 字节）
        ├─ 波形 → 一条 pulse：`{"channel": "A", "frames": ["0A0A0A0A00000000"], "duration_ms": 100}`
        └─ 强度 → 一条 intensity op（add / sub→add 负数 / set）

BF（软上限 + 平衡参数）**没有 V4 对应物** —— 它是"主机本地设置"，由
:class:`agent.nodes.sim_coyote.protocol.CoyoteState` 自己消化，不往设备发。

本模块是**纯函数**，不碰蓝牙、不碰 Zenoh，可单独单测。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from . import protocol as P

__all__ = [
    "B0_INTERVAL_MS",
    "PulseFrame",
    "IntensityOp",
    "b0_pulses",
    "b0_intensity",
    "b0_to_payload",
    "channel_bytes",
]

#: B0 的发送周期（协议规定每 100ms 一条，4 组 × 25ms）
B0_INTERVAL_MS = 100


@dataclass
class PulseFrame:
    """一条"波形流"（对应 V4 的一个 `device.op` / AppendPulseData）。"""

    channel: str  # "A" | "B"
    frames: list[str]  # 8 字节 hex（V3 帧格式，如 "0A0A0A0A00000000"）
    duration_ms: int = B0_INTERVAL_MS

    def to_dict(self) -> dict[str, Any]:
        return {"channel": self.channel, "frames": list(self.frames), "duration_ms": self.duration_ms}


@dataclass
class IntensityOp:
    """一条强度变更（对应 V4 的 AddIntensity / SetIntensity）。

    ``op`` 只有两种：``"add"``（相对，**减少用负值** —— V4 的 `reduce_strength` 就是 `add(-n)`）
    与 ``"set"``（绝对）。
    """

    channel: str
    op: str  # "add" | "set"
    value: int

    def to_dict(self) -> dict[str, Any]:
        return {"channel": self.channel, "op": self.op, "value": self.value}


def channel_bytes(wave: P.WaveChannel) -> str:
    """一个通道的波形 → V4 的 8 字节 hex **帧**（频率×4 紧接强度×4，大写）。

    作废的通道返回空串（不输出）。B0 的字节布局与 V3 波形帧**本来就一致**，
    所以这里是"原样搬运 + 大写化"，没有任何换算。
    """
    if not wave.valid:
        return ""
    raw = bytes(wave.freq) + bytes(wave.amp)
    return raw.hex().upper()


def b0_pulses(cmd: P.B0Command, *, duration_ms: int = B0_INTERVAL_MS) -> list[PulseFrame]:
    """B0 → 每个**有效**通道一条 :class:`PulseFrame`。

    作废的通道（波形整组无效）不产生帧 —— 这正是"单通道输出"的表达方式。
    """
    out: list[PulseFrame] = []
    for ch, wave in (("A", cmd.wave_a), ("B", cmd.wave_b)):
        hexed = channel_bytes(wave)
        if hexed:
            out.append(PulseFrame(channel=ch, frames=[hexed], duration_ms=duration_ms))
    return out


def b0_intensity(cmd: P.B0Command) -> list[IntensityOp]:
    """B0 的强度意图 → V4 强度 op 列表（``mode=NONE`` 的通道不产出）。"""
    out: list[IntensityOp] = []
    for ch, mode, value in (("A", cmd.mode_a, cmd.strength_a), ("B", cmd.mode_b, cmd.strength_b)):
        if mode is P.StrengthMode.NONE:
            continue
        v = 0 if value > P.STRENGTH_MAX else value  # 越界按 0（与真机一致）
        if mode is P.StrengthMode.ADD:
            out.append(IntensityOp(channel=ch, op="add", value=v))
        elif mode is P.StrengthMode.SUB:
            out.append(IntensityOp(channel=ch, op="add", value=-v))  # V4 用负数表示减少
        else:  # SET
            out.append(IntensityOp(channel=ch, op="set", value=v))
    return out


def b0_to_payload(cmd: P.B0Command, *, ts: float | None = None) -> dict[str, Any]:
    """一条 B0 → ``command/pulse_raw`` 的 payload（**纯数据**，JSON-safe）。

    ``pulses`` 为空 = 这条 B0 只有强度变化（或什么都没变）；``intensity`` 为空 = 只有波形。
    """
    return {
        "ts": float(cmd.ts if ts is None else ts),
        "seq": cmd.seq,
        "pulses": [p.to_dict() for p in b0_pulses(cmd)],
        "intensity": [i.to_dict() for i in b0_intensity(cmd)],
    }


def bf_to_payload(cmd: P.BfCommand) -> dict[str, Any]:
    """BF → payload。**仅用于本机审计/持久化**（V4 链路上没有软上限这个概念）。"""
    return {"ts": float(cmd.ts), "kind": "bf", **cmd.to_dict()}


#: 便于调用方打时间戳（测试可注入）
def now_ms() -> float:
    return time.time() * 1000.0
