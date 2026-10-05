"""T4 演示**网页进程** —— 连本机 Zenoh，给浏览器一个开火面板。

与 ``tools/dev/coyote_lab.py``（设备侧：stub 服务端 + 节点 + 假 App）是**两个进程**，
通过 Zenoh 对接：

    浏览器 ──WS──► 本进程 ──command/fire──► coyote_v4 节点 ──► V4 服务端 ──► 假 App
                      ▲                                                          │
                      └──────────── event/fire（审计） ◄────────────────────────┘

⚠️ 启动方式**必须**用 ``uvicorn.run(...)``（与 ``web/pose-preview/server.py`` 一致）。
实测：在已有的 asyncio loop 里 ``await uvicorn.Server(config).serve()`` 会让**所有 WS 握手 403**
（请求连 uvicorn 日志都进不去），排查代价极高。

用法::

    python tools/dev/coyote_lab.py --v4-port 9997      # 先起设备侧
    python web/coyote-lab/server.py --port 8770        # 再起本页
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

from agent.core import topics  # noqa: E402
from agent.core.config import Config, force_utf8  # noqa: E402

log = logging.getLogger("t4web")
_HTML = Path(__file__).resolve().parent / "index.html"

app = FastAPI(title="CoyoteLink T4 Lab (web)")

_room = "local"
_session: Any = None
_loop: Any = None          # ★ startup 时保存的 uvicorn 事件循环
_clients: set[WebSocket] = set()
_fired: list[dict] = []


def _load_waveforms() -> list[dict]:
    """官方波形 + data/waveforms 下导入的波形。"""
    try:
        from agent.nodes.coyote_v4 import waveform_io

        cfg = Config.load()
        table = waveform_io.load_all(cfg.data_dir / "waveforms")
    except Exception as exc:  # noqa: BLE001
        log.warning("加载波形失败：%s", exc)
        return []
    return [
        {"name": w.name, "version": w.version, "source": w.source, "frames": len(w.frames)}
        for w in sorted(table.values(), key=lambda x: x.name)
    ]


@app.on_event("startup")
async def _startup() -> None:
    import zenoh

    global _session, _loop
    # ★ 必须在 startup 里拿 running loop 并保存：Zenoh 回调跑在**非主线程**，
    #   在那里调 get_event_loop() 拿不到 uvicorn 的 loop（异常会被吞，表现为“广播没反应”）。
    _loop = asyncio.get_running_loop()
    _session = zenoh.open(zenoh.Config())
    key = f"{topics.PREFIX}/{_room}/{topics.EVENT_FIRE}"
    _session.declare_subscriber(key, _on_fire)
    log.info("已订阅 %s", key)


def _on_fire(sample: Any) -> None:
    """Zenoh 回调（**非主线程**）→ 丢回事件循环广播。"""
    try:
        rec = json.loads(bytes(sample.payload).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return
    _fired.append(rec)
    if _loop is None:
        return
    try:
        _loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_broadcast({"type": "fired", "record": rec})))
    except Exception as exc:  # noqa: BLE001
        log.warning("广播开火记录失败：%s", exc)


async def _broadcast(msg: dict) -> None:
    dead = []
    for ws in list(_clients):
        try:
            await ws.send_text(json.dumps(msg, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


@app.get("/api/pairing")
async def pairing():  # noqa: ANN202
    """真机模式的配对信息（由 tools/dev/coyote_lab.py --lan 写入）。"""
    p = ROOT / "data" / "t4_pairing.json"
    if not p.is_file():
        return {"available": False, "hint": "用 python tools/dev/coyote_lab.py --lan 启动设备侧"}
    try:
        info = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "hint": f"配对文件读不了：{exc}"}
    info["available"] = True
    return info


@app.get("/api/qr.png")
async def qr_png():  # noqa: ANN202
    """把配对链接生成二维码 PNG（手机用 DG-LAB App 扫）。"""
    import io

    import qrcode  # type: ignore
    from fastapi.responses import Response

    info = await pairing()
    if not info.get("available"):
        return Response(status_code=404)
    img = qrcode.make(info["qrcode"])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/")
async def index():  # noqa: ANN202
    return FileResponse(_HTML)


@app.get("/api/state")
async def state():  # noqa: ANN202
    return {
        "waveforms": _load_waveforms(),
        "fired_count": len(_fired),
        "room": _room,
    }


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    _clients.add(ws)
    await ws.send_text(json.dumps(
        {"type": "state", "waveforms": _load_waveforms(), "fired_count": len(_fired)},
        ensure_ascii=False))
    for rec in _fired[-30:]:
        await ws.send_text(json.dumps({"type": "fired", "record": rec}, ensure_ascii=False))
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            if msg.get("type") == "fire":
                if _session is None:
                    await ws.send_text(json.dumps({"type": "fire_result", "ok": False,
                                                   "error": "Zenoh 未就绪"}))
                    continue
                body = {
                    "waveform": msg.get("waveform", "PULSE"),
                    "channel": msg.get("channel", "A"),
                    "duration_ms": int(msg.get("duration_ms", 2000)),
                    "priority": int(msg.get("priority", 1)),
                    "overlay": bool(msg.get("overlay", True)),
                }
                # “一键开火”的强度部分（二选一）
                if msg.get("intensity_delta") is not None:
                    body["intensity_delta"] = int(msg["intensity_delta"])
                if msg.get("intensity") is not None:
                    body["intensity"] = int(msg["intensity"])
                key = f"{topics.PREFIX}/{_room}/{topics.COMMAND_FIRE}"
                _session.put(key, json.dumps(body, ensure_ascii=False).encode("utf-8"))
                log.info("→ 已发 command/fire: %s", body)
                await ws.send_text(json.dumps({"type": "fire_result", "ok": True, "sent": body},
                                              ensure_ascii=False))
            elif msg.get("type") == "state":
                await ws.send_text(json.dumps(
                    {"type": "state", "waveforms": _load_waveforms(), "fired_count": len(_fired)},
                    ensure_ascii=False))
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning("WS 异常：%s", exc)
    finally:
        _clients.discard(ws)


def main() -> None:
    force_utf8()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="T4 演示网页后端")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8770)
    args = ap.parse_args()

    import uvicorn

    log.info("T4 演示页：http://%s:%d", args.host, args.port)
    # ★ 必须用 uvicorn.run()（不要手动 Server(..).serve()）—— 见文件头注释
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
