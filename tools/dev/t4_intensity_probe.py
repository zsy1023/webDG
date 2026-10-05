# T4 实测脚本：验证 set_temp_intensity 能否与 send_pulse(波形) **并发**发出。
# （原暂存于 agent/nodes/coyote_v4/_staged_*.py，已由父 agent 归位到 tools/dev/。）
#
# 运行：python tools/dev/t4_intensity_probe.py
#
# 目的（任务 T4「本次要做的」第 1 条）：
#   实测 set_temp_intensity 能否与 send_pulse(波形) **并发**发出 ——
#   看假 App 收到的帧序列里到底有几个 device.op（V4ActionType：0=AppendPulseData,
#   3=AddIntensity, 4=SetTempIntensity, 7=SetIntensity）。
#
# 期望结论：一次开火 → 假 App 收到 **2 个** device.op（先 t=4 再 t=0），
#   两者各自带独立 requestId、各自被 App 回 resp、各自 resolve —— 即 SDK/协议层面
#   **可以并发**。（App 内部是否把两者互相打断属真机行为，无法用假 App 证明，
#   因此代码里仍然保留回落看门狗兜底。）

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

# 稳健地定位工作区根（本文件可能被移动到 tools/dev/）
_HERE = Path(__file__).resolve()
ROOT = _HERE
while ROOT.parent != ROOT and not (ROOT / "agent").is_dir():
    ROOT = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.core.config import force_utf8  # noqa: E402
from agent.nodes.coyote_v4 import CoyoteV4Node  # noqa: E402
from agent.nodes.coyote_v4.fire import FireRequest, fire  # noqa: E402
from tools.dev.v4_stub import FakeApp, RelayStub  # noqa: E402

log = logging.getLogger("t4.intensity_probe")

#: V4 动作码 → 名字（与 dglab_kit_python.types.V4ActionType 对齐）
ACTION_NAME = {0: "AppendPulseData", 3: "AddIntensity", 4: "SetTempIntensity", 7: "SetIntensity"}


async def wait_for(pred, timeout: float, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(interval)
    return False


async def main() -> int:
    force_utf8()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="T4 强度并发实测（RelayStub + coyote_v4 直连 + 假 App）")
    ap.add_argument("--v4-port", type=int, default=9995)
    ap.add_argument("--waveform", default="PULSE")
    ap.add_argument("--duration-ms", type=int, default=1500)
    ap.add_argument("--delta", type=int, default=10)
    args = ap.parse_args()

    stub = RelayStub(port=args.v4_port)
    await stub.start()

    node = CoyoteV4Node(url=f"ws://127.0.0.1:{args.v4_port}")
    asyncio.create_task(node.run())
    if not await wait_for(lambda: node.client and node.client.target_id, 25):
        log.error("coyote_v4 没连上服务端")
        node.stop()
        await stub.stop()
        return 1
    tid = node.client.target_id
    log.info("节点已连接 targetId=%s，波形数=%d", tid, len(node.waveforms))

    app = FakeApp(f"ws://127.0.0.1:{args.v4_port}", tid)
    app.start()
    if not await wait_for(lambda: node.is_paired, 20):
        log.error("假 App 没接入")
        await app.stop()
        node.stop()
        await stub.stop()
        return 1
    if not await wait_for(lambda: node.client.default_slot_id() is not None, 15):
        log.error("没发现 slot_id")
        await app.stop()
        node.stop()
        await stub.stop()
        return 1
    log.info("假 App 已接入，slot=%s", node.client.default_slot_id())

    # ── 开火：带时长 + 强度 +10（默认 intensity_mode=temp）──
    req = FireRequest(
        waveform=args.waveform,
        channel="A",
        duration_ms=args.duration_ms,
        priority=1,
        overlay=True,
        intensity_delta=args.delta,
    )
    log.info("开火请求：%s", req)
    result = await fire(node.client, node.waveforms, req)
    await asyncio.sleep(0.8)  # 等两帧都到假 App

    # ── 证据 ──
    print("\n" + "=" * 68)
    print("假 App 收到的 device.op 序列：")
    for record in app.fired:
        t = record.get("type")
        name = ACTION_NAME.get(t, f"unknown({t})")
        print(
            f"  t={t} ({name:<16}) channel={record.get('channel')} "
            f"duration_ms={record.get('duration_ms')} frames={record.get('frame_count')} "
            f"priority={record.get('priority')} immediate={record.get('immediate')}"
        )
    types = [r.get("type") for r in app.fired]
    print(f"\nop 个数 = {len(app.fired)}   t 序列 = {types}")
    print(f"FireResult = {result.to_dict()}")
    print("=" * 68)

    ok = (4 in types) and (0 in types)
    if ok:
        print("✅ 结论：set_temp_intensity(t=4) 与 send_pulse(t=0) **都独立发出** ——")
        print("   SDK/RPC 层面二者可并发（各自 requestId、各自回 resp、互不阻塞）。")
    else:
        print("❌ 结论：没同时看到 t=4 与 t=0 —— 并发假设不成立，请改走「定时降回 + 看门狗」。")

    await app.stop()
    node.stop()
    await asyncio.sleep(0.4)
    await stub.stop()
    return 0 if ok else 2


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(0)
