"""T4 演示后端 —— stub V4 服务端 + 直连的 ``coyote_v4`` 节点 + 假 App。

跑起来的效果：

    网页 / 命令行发 command/fire
        → coyote_v4 节点（官方 SDK，开火）
        → stub V4 服务端（透传）
        → 假 App（打印收到的波形帧）

用法（项目根目录）::

    python tools/dev/coyote_demo.py            # 起服务端+节点+假App，等配对
    python tools/dev/coyote_demo.py --fire PULSE --duration 2000   # 配对后自动开一枪

**这是临时联调工具，不是生产代码。** 真机上应把 ``coyote_v4`` 指向真实 V4 服务端。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.core.config import force_utf8  # noqa: E402
from agent.core import topics  # noqa: E402
from tools.dev.v4_stub import FakeApp, RelayStub  # noqa: E402

log = logging.getLogger("t4demo")


async def wait_for(pred, timeout: float, interval: float = 0.2) -> bool:
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

    ap = argparse.ArgumentParser(description="T4 演示：stub V4 服务端 + coyote_v4 节点 + 假 App")
    ap.add_argument("--port", type=int, default=9998)
    ap.add_argument("--room", default="local")
    ap.add_argument("--fire", default=None, help="配对成功后自动开火这个波形名")
    ap.add_argument("--channel", default="A")
    ap.add_argument("--duration", type=int, default=2000)
    ap.add_argument("--seconds", type=int, default=0, help=">0 则倒计时后自动退出")
    args = ap.parse_args()

    url = f"ws://127.0.0.1:{args.port}"

    # 1) stub V4 服务端
    stub = RelayStub(port=args.port)
    await stub.start()

    # 2) coyote_v4 节点（直连模式）
    from agent.nodes.coyote_v4 import CoyoteV4Node

    node = CoyoteV4Node(url=url)
    node_task = asyncio.create_task(node.run())

    # 3) 等服务端分配 targetId
    ok = await wait_for(lambda: node.client is not None and node.client.target_id, timeout=20)
    if not ok:
        log.error("coyote_v4 没能在 20s 内连上 stub 服务端")
        node.stop()
        await stub.stop()
        return 1
    tid = node.client.target_id
    log.info("✅ 控制方已连接，targetId=%s", tid)

    # 4) 假 App 接入
    app = FakeApp(url, tid)
    app.start()

    paired = await wait_for(lambda: node.is_paired, timeout=20)
    log.info("%s 配对状态=%s", "✅" if paired else "❌", node.is_paired)
    if not paired:
        node.stop()
        await stub.stop()
        return 1

    # 5) 拉起一个 Zenoh 发布者，用来发 command/fire
    import zenoh

    session = zenoh.open(zenoh.Config())
    pub = session.declare_publisher(f"{topics.PREFIX}/{args.room}/{topics.COMMAND_FIRE}")
    events: list[dict] = []

    def on_event(sample):  # noqa: ANN001
        try:
            events.append(json.loads(bytes(sample.payload).decode("utf-8")))
        except Exception:  # noqa: BLE001
            pass

    sub = session.declare_subscriber(f"{topics.PREFIX}/{args.room}/{topics.EVENT_FIRE}", on_event)

    async def shoot(name: str) -> None:
        payload = {
            "waveform": name, "channel": args.channel,
            "duration_ms": args.duration, "priority": 1, "overlay": True,
        }
        log.info("→ 发 command/fire: %s", payload)
        pub.put(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        await wait_for(lambda: bool(app.fired), timeout=10)

    if args.fire:
        await shoot(args.fire)
        await asyncio.sleep(0.5)
        log.info("─" * 60)
        log.info("假 App 收到 %d 次开火：", len(app.fired))
        for r in app.fired:
            log.info("   %s", r)
        log.info("节点发出的 event/fire 审计：%d 条", len(events))
        for e in events:
            log.info("   %s", e)

    if args.seconds > 0:
        log.info("演示运行 %ds（Ctrl+C 提前结束）…", args.seconds)
        await asyncio.sleep(args.seconds)
    else:
        log.info("保持运行中（Ctrl+C 结束）…")
        await asyncio.Event().wait()

    await app.stop()
    node.stop()
    await wait_for(lambda: not node_task.done(), timeout=0.1, interval=0.01)
    session.close()
    await stub.stop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(0)
