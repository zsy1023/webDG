# -*- coding: utf-8 -*-
"""T9 端到端实测：模拟主机截获的 B0 → `command/pulse_raw` → coyote_v4 → 被控端收到 device.op。

三个真实节点（都在本机 Zenoh 上跑）：
    SimCoyoteNode(loopback 后端)  ──pulse_raw──>  CoyoteV4Node  ──V4 socket──>  假 App

**2026-10-04 实测结果**（喂 `b0 1c 0a 00 0a0a0a0a 00000000 00000000 00000065`，
即 seq=1 / SET A=10 / A 通道波形有效 / B 整组作废）—— 被控端收到 **3 条 device.op**：

    AppendPulseData  d=100  v=['0A0A0A0A00000000']  ver=3   ← 波形帧**原样**送达
    SetIntensity     v=0                                    ← set 由 reset+add 两步实现
    AddIntensity     v=10                                   ← 强度 10

用法：``python tools/dev/pulse_raw_e2e.py``（不需要任何硬件）。
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]  # <repo>/tools/dev/xxx.py → <repo>
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.core.config import force_utf8  # noqa: E402
from agent.nodes.coyote_v4 import CoyoteV4Node  # noqa: E402
from agent.nodes.sim_coyote import SimCoyoteNode  # noqa: E402
from tools.dev.v4_stub import FakeApp, RelayStub  # noqa: E402

PORT = 9994
#: seq=1 / mode=SET(A) + NONE(B) / 强度设定 A=10 / A 波形 10,10,10,10 + 幅度 0 / B 整组作废
B0 = bytes.fromhex("b0 1c 0a 00  0a0a0a0a  00000000  00000000  00000065")


async def wait_for(pred, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


async def main() -> int:
    force_utf8()
    stub = RelayStub(port=PORT)
    await stub.start()

    node_v4 = CoyoteV4Node(url=f"ws://127.0.0.1:{PORT}")
    task_v4 = asyncio.create_task(node_v4.run())
    if not await wait_for(lambda: node_v4.client and node_v4.client.target_id, 25):
        print("✗ coyote_v4 没连上")
        return 1
    tid = node_v4.client.target_id
    print(f"① coyote_v4 已连（targetId={tid}）")

    received: list[dict] = []

    def on_pulse(record: dict) -> None:
        received.append(record)

    app = FakeApp(f"ws://127.0.0.1:{PORT}", tid, on_pulse=on_pulse)
    app.start()
    if not await wait_for(lambda: node_v4.is_paired, 25):
        print("✗ 假 App 没配对")
        return 1
    print(f"② 假 App 已配对（波形表 {len(node_v4.waveforms)} 条）")

    node_sim = SimCoyoteNode(backend="loopback")
    task_sim = asyncio.create_task(node_sim.run())
    await wait_for(lambda: node_sim.backend is not None, 15)
    await asyncio.sleep(1.5)  # 等 Zenoh 订阅关系建好
    print(f"③ sim_coyote 已起（后端={node_sim.backend.kind if node_sim.backend else '?'}）")

    print(f"\n④ 往模拟主机喂一条真实 B0：{B0.hex(' ')}")
    node_sim.backend.feed(B0)

    got = await wait_for(lambda: bool(received), 6)
    print(f"\n⑤ 被控端收到了 {len(received)} 条 device.op" if got else "\n✗ 被控端什么都没收到")
    for r in received[:6]:
        print("   " + str(r)[:260])

    print(f"\n（sim_coyote 统计：{node_sim.stats}）")

    for t in (task_sim, task_v4):
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t
    node_sim.stop()
    node_v4.stop()
    await stub.stop()
    return 0 if got else 2


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
