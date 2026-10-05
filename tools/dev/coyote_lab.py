"""T4 演示**设备侧** —— V4 服务端 + 直连的 coyote_v4 节点 +（可选）真机/假 App。

两种模式：

1. **假 App 模式**（默认）—— 内置 ``FakeApp`` 顶替被控方，不需要任何硬件，用来自测协议链路。
2. **真机模式**（``--lan``）—— 服务端监听 ``0.0.0.0``，生成配对二维码。
   **你手机上的 DG-LAB App 扫码接入**（当被控方），App 再蓝牙连真实郊狼主机，
   于是网页上点"开火"就是**真设备**在输出。

    python tools/dev/coyote_lab.py --lan                 # 打印二维码 URL
    python tools/dev/coyote_lab.py --lan --fake-app      # 同时留一个假 App 方便自测

    ⚠️ 真机模式要求手机和电脑在**同一局域网**；App 扫码的 ws URL 用的是本机局域网 IP。
    若 App 拒绝非 wss，需要用樱花穿透 + 证书（那属于 T3/部署的范畴）。

网页部分照常：``python web/coyote-lab/server.py --port 8770``
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.core.config import force_utf8  # noqa: E402
from tools.dev.v4_stub import FakeApp, RelayStub  # noqa: E402

log = logging.getLogger("t4lab")
_PAIRING_FILE = ROOT / "data" / "t4_pairing.json"


def lan_ip() -> str:
    """取本机在局域网上最可能被手机访问到的 IPv4。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # 不发包，只为让内核选出口网卡
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


def make_pairing(host: str, port: int, tid: str, room: str | None = None) -> dict:
    """按 dglab-kit 的规则拼配对二维码 URL。

    ``room`` 是**房间口令**：带了就拼进 query（App 会原样连过去），中继那边会校验 ——
    这样**二维码本身就是"带口令的连接"**，用户不用手填。
    """
    ws_url = f"ws://{host}:{port}?tid={tid}"
    if room:
        ws_url += f"&room={room}"
    qrcode = f"https://dungeon-lab.cn/s/?v=1&action=socket&url={quote(ws_url, safe='')}"
    return {"ws_url": ws_url, "qrcode": qrcode, "target_id": tid, "room": room,
            "host": host, "port": port, "ts": int(time.time() * 1000)}


def write_pairing(info: dict) -> None:
    _PAIRING_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PAIRING_FILE.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")


async def wait_for(pred, timeout: float, interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(interval)
    return False


async def main() -> int:
    force_utf8()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="T4 设备侧（V4 服务端 + coyote_v4 节点）")
    ap.add_argument("--v4-port", type=int, default=9997)
    ap.add_argument("--room", default="local")
    ap.add_argument("--lan", action="store_true",
                    help="监听 0.0.0.0 并生成配对二维码（手机可扫）")
    ap.add_argument("--public-host", default=None,
                    help="二维码里用的**对外地址**（默认自动取局域网 IP）。"
                         "走樱花穿透时填它分配的公网域名，如 xxxxx.natfrp.com")
    ap.add_argument("--public-port", type=int, default=None,
                    help="二维码里用的**对外端口**（樱花映射出来的那个端口）")
    ap.add_argument("--room", default=None,
                    help="房间口令（要与 tools/relay.py --room 一致；会自动拼进二维码）")
    ap.add_argument("--fake-app", action="store_true",
                    help="即使 --lan 也额外起一个内置假 App（自测用）")
    args = ap.parse_args()

    bind = "0.0.0.0" if args.lan else "127.0.0.1"
    stub = RelayStub(host=bind, port=args.v4_port)
    await stub.start()

    from agent.nodes.coyote_v4 import CoyoteV4Node

    node = CoyoteV4Node(url=f"ws://127.0.0.1:{args.v4_port}")
    asyncio.create_task(node.run())

    if not await wait_for(lambda: node.client and node.client.target_id, 25):
        log.error("coyote_v4 没连上服务端")
        node.stop()
        await stub.stop()
        return 1
    tid = node.client.target_id

    import zenoh

    session = zenoh.open(zenoh.Config())

    if args.lan:
        # 二维码里必须是**手机能访问到的地址**：局域网演示自动取本机 IP；
        # 走樱花穿透时用 --public-host/--public-port 填公网那个。
        host = args.public_host or lan_ip()
        port = args.public_port or args.v4_port
        info = make_pairing(host, port, tid, args.room)
        write_pairing(info)
        print()
        print("=" * 68)
        print("  真机模式：用手机 DG-LAB App 扫下面这个链接（把它生成二维码）")
        print(f"    {info['qrcode']}")
        print()
        print(f"  控制方 targetId : {tid}")
        print(f"  服务端 ws 地址  : {info['ws_url']}   （监听 {bind}:{args.v4_port}）")
        print(f"  已写入          : {_PAIRING_FILE.relative_to(ROOT)}（网页会显示二维码）")
        print("=" * 68)
        print()
        log.info("等待真机接入…（App 扫码后这里会打 被控方已接入）")

    if (not args.lan) or args.fake_app:
        def on_pulse(record: dict) -> None:
            log.info("🔥 假 App 收到开火：%s", record)

        app = FakeApp(f"ws://127.0.0.1:{args.v4_port}", tid, on_pulse=on_pulse)
        app.start()
        await wait_for(lambda: node.is_paired, 20)
        log.info("假 App 已接入，配对=%s", node.is_paired)

    log.info("设备侧就绪。网页： python web/coyote-lab/server.py --port 8770")
    try:
        await asyncio.Event().wait()
    finally:
        node.stop()
        session.close()
        await stub.stop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(0)
