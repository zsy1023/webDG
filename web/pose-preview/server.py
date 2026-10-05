"""CoyoteLink 骨骼预览（最小页）—— 后端桥。

做三件事（保持最小）：

1. 连本机 Zenoh，订阅 ``feature/pose`` / ``feature/face`` / ``feature/audio`` / ``event/device``；
2. 把收到的特征**解码成可读 JSON** 推给浏览器 WebSocket；
3. 把浏览器选的设备回发 ``command/select_device``（device / camera / audio 节点各自响应）。

隐私（需求 1d）：这里**只搬结构化特征**。``feature/frame.raw``（原始帧）根本不在订阅列表里；
浏览器拿到的是坐标 / 角度 / 音量这些 JSON，**永远没有图像或音频字节**。

运行（在项目根目录）::

    python web/pose-preview/server.py            # 默认 http://127.0.0.1:8765
    python web/pose-preview/server.py --port 9000

配套节点::

    python -m agent.core.cli run camera     # 另开窗口
    python -m agent.core.cli run pose
    python -m agent.core.cli run device     # 设备下拉框要用它
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

# 让 `agent` 可导入（允许从任意 cwd 启动）
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import zenoh  # noqa: E402
from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402

from agent.core import topics  # noqa: E402
from agent.core.config import force_utf8  # noqa: E402
from agent.nodes.pose.frame_codec import CodecError, decode_frame  # noqa: E402

_HTML = Path(__file__).resolve().parent / "index.html"


def _decode_json(data: bytes) -> Any:
    try:
        return json.loads(bytes(data).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


class Hub:
    """极简广播中心：Zenoh 回调线程 → asyncio 事件循环 → 所有 WS 客户端。"""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[WebSocket] = set()
        self.last: dict[str, Any] = {}  # 最近一条各类型消息，供新连入的客户端补齐

    async def register(self, ws: WebSocket) -> None:
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
        await ws.accept()
        self.clients.add(ws)
        for payload in self.last.values():
            try:
                await ws.send_text(json.dumps(payload, ensure_ascii=False))
            except Exception:  # noqa: BLE001
                pass

    def unregister(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    def broadcast(self, payload: dict[str, Any]) -> None:
        self.last[payload.get("type", "?")] = payload
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        text = json.dumps(payload, ensure_ascii=False)
        asyncio.run_coroutine_threadsafe(self._send_all(text), loop)

    async def _send_all(self, text: str) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.clients):
            try:
                await ws.send_text(text)
            except Exception:  # noqa: BLE001 - 断开的客户端
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


def build_app(hub: Hub, room: str, publisher: Publisher) -> FastAPI:
    app = FastAPI(title="CoyoteLink 骨骼预览", docs_url=None, redoc_url=None)

    @app.get("/")
    async def index() -> Any:
        if not _HTML.exists():
            return JSONResponse({"error": "index.html 缺失"}, status_code=500)
        return FileResponse(str(_HTML))

    @app.get("/healthz")
    async def healthz() -> Any:
        return {"ok": True, "clients": len(hub.clients), "room": room}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await hub.register(ws)
        publisher.trigger_list()  # 让 device 节点补发一次设备快照
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                await _handle_client(hub, publisher, msg)
        except WebSocketDisconnect:
            pass
        finally:
            hub.unregister(ws)

    return app


async def _handle_client(hub: Hub, publisher: Any, msg: Any) -> None:
    if not isinstance(msg, dict):
        return
    op = msg.get("op")
    if op == "list":
        # 请求设备快照：转成一条选择指令，device 节点会重发 event/device
        publisher.trigger_list()
    elif op == "select":
        kind = msg.get("kind")
        device_id = msg.get("id")
        if kind and device_id:
            publisher.select(str(kind), str(device_id))


class Publisher:
    """包一层 Zenoh 发布/订阅，key 全部来自 topics（禁止裸 key 字符串）。"""

    def __init__(self, session: Any, hub: Hub, room: str) -> None:
        self.session = session
        self.hub = hub
        self.room = room
        self._select = session.declare_publisher(topics.key(topics.COMMAND_SELECT_DEVICE, room))
        self._subs = [
            session.declare_subscriber(topics.key(topics.FEATURE_POSE, room), self._on_pose),
            session.declare_subscriber(topics.key(topics.FEATURE_FACE, room), self._on_face),
            session.declare_subscriber(topics.key(topics.FEATURE_AUDIO, room), self._on_audio),
            session.declare_subscriber(topics.key(topics.EVENT_DEVICE, room), self._on_device),
        ]

    # —— 浏览器 → 节点 ——
    def select(self, kind: str, device_id: str) -> None:
        self._select.put(
            json.dumps({"kind": kind, "id": device_id, "action": "select"}, ensure_ascii=False).encode("utf-8")
        )

    def trigger_list(self) -> None:
        self._select.put(json.dumps({"action": "list"}, ensure_ascii=False).encode("utf-8"))

    # —— 节点 → 浏览器 ——
    def _on_pose(self, sample: Any) -> None:
        try:
            frame = decode_frame(bytes(sample.payload))
        except CodecError:
            return
        self.hub.broadcast(
            {
                "type": "pose",
                "ts": frame.ts,
                "seq": frame.seq,
                "present": frame.present,
                "landmarks": frame.landmarks,
                "visibility": frame.visibility,
            }
        )

    def _on_face(self, sample: Any) -> None:
        data = _decode_json(sample.payload)
        if isinstance(data, dict):
            data["type"] = "face"
            self.hub.broadcast(data)

    def _on_audio(self, sample: Any) -> None:
        data = _decode_json(sample.payload)
        if isinstance(data, dict):
            data["type"] = "audio"
            self.hub.broadcast(data)

    def _on_device(self, sample: Any) -> None:
        data = _decode_json(sample.payload)
        if isinstance(data, dict):
            data["type"] = "devices"
            self.hub.broadcast(data)

    def close(self) -> None:
        for sub in self._subs:
            try:
                sub.undeclare()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._select.undeclare()
        except Exception:  # noqa: BLE001
            pass


def _free_port(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(description="CoyoteLink 骨骼预览页（T2）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--room", default=os.environ.get("COYOTELINK_ROOM", "local"))
    args = parser.parse_args(argv)

    import uvicorn

    hub = Hub()
    session = zenoh.open(zenoh.Config())
    publisher = Publisher(session, hub, args.room)
    app = build_app(hub, args.room, publisher)

    print(f"# 骨骼预览：http://{args.host}:{args.port}  （订阅 room={args.room}）", flush=True)
    print("# 记得另开窗口跑：python -m agent.core.cli run camera / run pose / run device", flush=True)
    if not _free_port(args.host, args.port):
        print(f"# ⚠ 端口 {args.port} 似乎已被占用", flush=True)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        publisher.close()
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
