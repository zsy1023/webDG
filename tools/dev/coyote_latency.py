# -*- coding: utf-8 -*-
"""郊狼控制链路 —— 本机段延迟实测（结果见 docs/research/郊狼控制链路延迟实测.md）。

完整链路（从"点开火"到"真设备输出"）：

    web面板/rules ──①Zenoh command/fire──> coyote_v4 ──②组帧+SDK发送──>
    ──③V4 socket──> 被控端 App ──④BLE──> 真郊狼

本脚本测 **①②③ 的本机段**（本地 relay + 假 App，不需要真设备、不需要手机）：

    L0  Zenoh：publish command/fire → 裸订阅者收到
    L1  端到端（本机段）：publish → 被控方收到 device.op（含组帧 + SDK 发送 + 服务端转发）

④（BLE）用 ``tools/dev/coyote_ble_latency.py`` 单独实测（需要真机在范围内）。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import statistics
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]  # <repo>/tools/dev/xxx.py → <repo>
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.core import topics  # noqa: E402
from agent.core.config import force_utf8  # noqa: E402
from agent.nodes.coyote_v4 import CoyoteV4Node  # noqa: E402
from tools.dev.v4_stub import FakeApp, RelayStub  # noqa: E402

PORT = 9995
ROUNDS = 15


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100.0 * (len(xs) - 1))))]


def report(name: str, xs: list[float]) -> None:
    if not xs:
        print(f"{name}: 无样本")
        return
    print(
        f"{name}\n"
        f"    n={len(xs)}  min={min(xs):.1f}  p50={pct(xs, 50):.1f}  p90={pct(xs, 90):.1f}  "
        f"p99={pct(xs, 99):.1f}  max={max(xs):.1f}  mean={statistics.mean(xs):.1f}  (ms)"
    )


async def wait_for(pred, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


async def main() -> int:
    force_utf8()
    import zenoh

    stub = RelayStub(port=PORT)
    await stub.start()
    node = CoyoteV4Node(url=f"ws://127.0.0.1:{PORT}")
    node_task = asyncio.create_task(node.run())
    if not await wait_for(lambda: node.client and node.client.target_id, 25):
        print("✗ coyote_v4 没连上本地服务端")
        return 1
    tid = node.client.target_id
    print(f"coyote_v4 已连：targetId={tid}")

    # 假 App = 被控方（它自己回 resp）
    rx: dict[str, int] = {}

    def on_pulse(record: dict) -> None:
        rx.setdefault("app", time.perf_counter_ns())

    app = FakeApp(f"ws://127.0.0.1:{PORT}", tid, on_pulse=on_pulse)
    app.start()
    paired = await wait_for(lambda: node.is_paired, 25)
    print(f"假 App 配对={paired}  波形数={len(node.waveforms)}  波形样本={list(node.waveforms)[:6]}")
    if not paired or not node.waveforms:
        print("✗ 没配对或没有波形，测不了")
        node.stop()
        await stub.stop()
        return 1

    # 裸订阅（测 L0）
    session = zenoh.open(zenoh.Config())
    key_fire = topics.command(topics.COMMAND_FIRE)
    l0: list[float] = []
    box: dict[str, int] = {}

    def on_raw(_s) -> None:
        t0 = box.pop("t0", None)
        if t0 is not None:
            l0.append((time.perf_counter_ns() - t0) / 1e6)

    sub = session.declare_subscriber(key_fire, on_raw)
    await asyncio.sleep(1.5)
    pub = session.declare_publisher(key_fire)
    await asyncio.sleep(0.5)

    wave = next(iter(node.waveforms))
    payload = {"waveform": wave, "channel": "A", "duration_ms": 800,
               "priority": 1, "overlay": True}
    print(f"\n开始 {ROUNDS} 轮：波形={wave}\n")

    l1: list[float] = []
    for i in range(ROUNDS):
        rx.clear()
        t0 = time.perf_counter_ns()
        box["t0"] = t0
        pub.put(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        got = await wait_for(lambda: "app" in rx, 4.0)
        if got:
            l1.append((rx["app"] - t0) / 1e6)
            print(f"  轮 {i + 1:2d}: 假App 收到 device.op  {(rx['app'] - t0) / 1e6:7.1f} ms")
        else:
            print(f"  轮 {i + 1:2d}: 超时（假 App 没收到）")
        await asyncio.sleep(0.35)

    print()
    report("L0  Zenoh: publish command/fire → 裸订阅者", l0)
    print()
    report("L1  端到端（本机段）: publish → 被控方收到开火帧", l1)
    print(f"\n（假 App 共收到 {len(app.fired)} 次开火）")

    session.close()
    node.stop()
    node_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await node_task
    await stub.stop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
