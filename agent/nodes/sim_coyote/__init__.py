"""``sim_coyote`` —— 模拟郊狼 BLE 外设（T1，控制侧）。

对外形态是 :class:`~agent.nodes.sim_coyote.node.SimCoyoteNode`。

模块地图::

    protocol.py           BLE V3 协议编解码 + 主机状态（**纯逻辑**，可脱离蓝牙单测）
    transport.py          BLE 外设后端（**可插拔**：esp32 / winrt / loopback）
    node.py               SimCoyoteNode —— 把两者接起来，发布 command/raw_v4
    probe_peripheral.py   T1 §9 可行性探针（Windows GattServiceProvider，**路线已否决**）

⚠️ **BLE 外设走 ESP32**：Windows 的 ``GattServiceProvider`` 发布不了保留服务
（``0x180A``/``0x1800`` → ``DISABLED_BY_POLICY``），详见 T1 任务书 §8。
"""

from __future__ import annotations

# protocol 必须先于 node —— node 用相对导入回引本包，这样能避免循环导入。
from agent.nodes.sim_coyote.protocol import (
    B0_LEN,
    B1_LEN,
    BF_LEN,
    B0Command,
    BfCommand,
    Command,
    CoyoteState,
    ProtocolError,
    StrengthMode,
    WaveChannel,
    build_b0,
    build_b1,
    build_bf,
    parse_b0,
    parse_b1,
    parse_bf,
    parse_command,
)
from agent.nodes.sim_coyote import v4_map
from agent.nodes.sim_coyote.node import SimCoyoteNode
from agent.nodes.sim_coyote.transport import (
    ClientInfo,
    Esp32Peripheral,
    LoopbackPeripheral,
    PeripheralBackend,
    PeripheralError,
    WinRtPeripheral,
    create_backend,
)

__all__ = [
    # protocol
    "B0Command",
    "BfCommand",
    "Command",
    "CoyoteState",
    "ProtocolError",
    "StrengthMode",
    "WaveChannel",
    "B0_LEN",
    "BF_LEN",
    "B1_LEN",
    "parse_b0",
    "build_b0",
    "parse_bf",
    "build_bf",
    "parse_b1",
    "build_b1",
    "parse_command",
    # transport
    "ClientInfo",
    "PeripheralBackend",
    "PeripheralError",
    "Esp32Peripheral",
    "WinRtPeripheral",
    "LoopbackPeripheral",
    "create_backend",
    # node
    "SimCoyoteNode",
    # T9 映射
    "v4_map",
]
