"""一次性诊断：command/fire 到底有没有在 Zenoh 上传出去。

同时做三件事，看谁收到了：
  A. 裸 Zenoh 订阅 coyotelink/local/command/fire   （绕过节点）
  B. 起一个直连的 coyote_v4 节点（带 stub + 假 App）
  C. 发一条 command/fire，看 A 和 B 各自的反应
"""

from __future__ import annotations

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("diag")


async def wait_for(pred, timeout: float, interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(interval)
    return False


async def main() -> int:
    force_utf8()
    import zenoh

    key = f"{topics.PREFIX}/local/{topics.COMMAND_FIRE}"
    print(f"目标 key = {key}")

    # C 的发布端
    session = zenoh.open(zenoh.Config())
    got_by_raw: list = []

    def on_raw(sample):
        got_by_raw.append(bytes(sample.payload))
        print("   [A 裸订阅] 收到:", bytes(sample.payload)[:100])

    sub = session.declare_subscriber(key, on_raw)
    print("A) 裸订阅已建立")

    # B) 直连节点
    stub = RelayStub(port=9996)
    await stub.start()
    from agent.nodes.coyote_v4 import CoyoteV4Node

    node = CoyoteV4Node(url="ws://127.0.0.1:9996")
    task = asyncio.create_task(node.run())
    await wait_for(lambda: node.client is not None and node.client.target_id, timeout=20)
    tid = node.client.target_id
    print(f"B) 节点已连接 targetId={tid}")
    app = FakeApp("ws://127.0.0.1:9996", tid)
    app.start()
    await wait_for(lambda: node.is_paired, timeout=20)
    print(f"B) 配对={node.is_paired}, 波形数={len(node.waveforms)}")

    # 等 Zenoh 把订阅关系建好
    await asyncio.sleep(2.0)

    payload = {"waveform": "PULSE", "channel": "A", "duration_ms": 2000, "priority": 1, "overlay": True}
    print(f"C) 发送: {payload}")
    pub = session.declare_publisher(key)
    await asyncio.sleep(0.5)
    pub.put(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    await wait_for(lambda: bool(app.fired), timeout=8)
    print("─" * 56)
    print(f"A 裸订阅收到 : {len(got_by_raw)} 条")
    print(f"B 假 App 收到: {len(app.fired)} 次  {app.fired}")
    print(f"  节点 is_paired={node.is_paired}  client_ids={getattr(node.client, 'client_ids', None)}")

    await app.stop()
    node.stop()
    await asyncio.sleep(0.5)
    session.close()
    await stub.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
