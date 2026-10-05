"""**临时的** V4 服务端 + 假 App —— 只用于 T4 演示/联调，**不是生产代码**。

为什么要它：``coyote_v4`` 是"控制方"，要真开枪就得有个 App 当"被控方"。
真机不在手边，就用这个假 App 顶上，把整条链路跑通：

    网页 → command/fire → coyote_v4 节点 → V4 服务端 → 假 App（打印波形）

协议依据 ``docs/架构设计与路线图.md`` §4.1（已从 ``v4-server.ts`` 核实）：

- 控制方连 ``ws://host:PORT``          → 收 ``hello{clientId}``
- 被控方连 ``ws://host:PORT?tid=<控制方id>`` → 收 ``hello`` + ``controller_attached``
- 服务端通知控制方 ``client_attached{clientId=<被控方id>}``
- 控制方 → ``{"type":"message","clientId":<被控方id>,"data":{...}}``
- 被控方 → ``{"type":"message","data":{...}}``（服务端补上 clientId 转给控制方）

假 App 还会在被控方接入后主动发 ``devices.snapshot``，并回应 ``device.op`` 请求。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = ["RelayStub", "FakeApp", "AccessRecord"]

log = logging.getLogger("t4demo.stub")


def peer_of(ws: Any) -> str:
    """取连接来源 ``ip:port``（websockets 把 remote_address 放在连接对象上）。"""
    addr = getattr(ws, "remote_address", None)
    if isinstance(addr, tuple) and addr:
        return f"{addr[0]}:{addr[1]}"
    return str(addr or "?")


@dataclass
class AccessRecord:
    """一次接入审计（需求 7「看到是谁访问了」）。"""

    at: float
    remote: str          # ip:port
    role: str            # "controller" | "client" | "rejected"
    room_ok: bool
    client_id: str = ""
    until: float | None = None
    reason: str = ""
    #: 握手 URL 的原始 path（含 query）—— 排"口令为什么不对"时**全靠它**：
    #: 2026-10-04 手机被 `bad_room` 拒掉时，就是靠这个字段才看清 App 到底带了什么。
    path: str = ""

    @property
    def seconds(self) -> float:
        return (self.until or time.time()) - self.at

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "at_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.at)),
            "remote": self.remote,
            "role": self.role,
            "room_ok": self.room_ok,
            "client_id": self.client_id,
            "until": self.until,
            "seconds": round(self.seconds, 2),
            "reason": self.reason,
            "path": self.path,
        }


async def _send_msg(ws: Any, payload: dict) -> None:
    """被控方 → 服务端的**帧封装**。

    ⚠️ 关键：SDK 只从 ``{"type":"message","data":…}`` 里取 payload。
    被控方若直接发事件对象（如 ``{"t":"ev",...}``）会被**静默忽略**，
    表现为 ``request_devices`` 超时、没有 slot_id、开火失败。
    """
    try:
        await ws.send(json.dumps({"type": "message", "data": payload}, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass

#: 模拟郊狼主机（SDK 的 DglabSocketDeviceType 是 StrEnum）
DEVICE_TYPE = "COYOTE_030"
SLOT_ID = "slot-sim-1"

#: 服务端主动 ping 的间隔（秒）—— 见 `RelayStub._keepalive_loop`
_KEEPALIVE_S = 15.0


class RelayStub:
    """最小 V4 中继：1 控制方 : 1 被控方，纯透传。

    :param room: **房间口令**（可选，但穿透到公网时**强烈建议给**）。
        给了就要求连接带口令，否则拒连（close 4403）。口令认三种形态（见
        :meth:`RelayStub._room_ok`）：``?room=`` / 路径段 ``/<口令>/`` /
        **被控方（带 tid）不带口令但 tid 命中在线控制方**（DG-LAB App 会丢掉我们
        附加的 query，实测就是这么把手机拒掉的）。
        就是"加个房间号"那层鉴权 —— 别让公网上的端口裸奔。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9998,
                 room: str | None = None) -> None:
        self.host = host
        self.port = port
        self.room = (room or "").strip() or None
        self._server: Any = None
        self.controllers: dict[str, Any] = {}   # clientId -> ws
        self.clients: dict[Any, Any] = {}       # 被控方 ws -> 控制方 ws
        self.client_ids: dict[Any, str] = {}    # 被控方 ws -> 自己的 clientId
        self.label: dict[Any, str] = {}         # ws -> 展示名（控制方 / 被控方）
        self._keepalive: Any = None             # 服务端主动 ping 的后台任务
        self.on_pulse: Any = None               # 收到 device.op 时的回调（给演示页用）
        #: 接入审计（谁什么时候连过）—— 需求 7「看到是谁访问了」
        self.access: list[AccessRecord] = []
        self.on_access: Any = None              # 每来一条就回调（relay.py 用它落盘）

    def _record(self, ws: Any, role: str, *, room_ok: bool,
                client_id: str = "", reason: str = "", path: str = "") -> AccessRecord:
        rec = AccessRecord(at=time.time(), remote=peer_of(ws), role=role,
                           room_ok=room_ok, client_id=client_id, reason=reason,
                           path=path or self._raw_path(ws))
        self.access.append(rec)
        if len(self.access) > 500:              # 只留最近若干条，别长跑爆内存
            del self.access[: len(self.access) - 500]
        if self.on_access is not None:
            with contextlib.suppress(Exception):
                self.on_access(rec)
        return rec

    @staticmethod
    def _close_record(rec: AccessRecord | None) -> None:
        if rec is not None and rec.until is None:
            rec.until = time.time()

    # ── 服务端生命周期 ────────────────────────────────────────
    async def start(self) -> None:
        import websockets

        async def handler(ws: Any) -> None:
            await self._handle(ws)

        self._server = await websockets.serve(handler, self.host, self.port)
        self._keepalive = asyncio.create_task(self._keepalive_loop())
        log.info("V4 stub 服务端已启动 ws://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        task, self._keepalive = self._keepalive, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _keepalive_loop(self) -> None:
        """周期性给两边发 ``{"type":"ping"}`` —— 官方 v4 服务端就是这么保活的。

        ★ 为什么要补：实测 DG-LAB App 接入后**每 ~30 秒就断开重连一次**
          （审计里同一个 tid 连着冒出好几个新 clientId），很像在等服务端心跳、
          等到超时。官方 `v4-server.ts` 里有 `startWsPing()`；我们这边原来只会
          **被动回** pong，从不主动 ping。
        """
        while True:
            await asyncio.sleep(_KEEPALIVE_S)
            for ws in (*self.controllers.values(), *self.clients.keys()):
                await self._send(ws, {"type": "ping", "ts": int(time.time() * 1000)})

    # ── 连接处理 ──────────────────────────────────────────────
    @staticmethod
    def _raw_path(ws: Any) -> str:
        """握手 URL 的原始 path（含 query）—— 兼容 websockets 不同版本拿 path 的方式。"""
        req = getattr(ws, "request", None)
        if req is not None and getattr(req, "path", None):
            return str(req.path)
        return str(getattr(ws, "path", "") or "")

    @classmethod
    def _query_of(cls, ws: Any) -> dict[str, str]:
        """取握手 URL 的 query（兼容 websockets 不同版本拿 path 的方式）。"""
        raw = cls._raw_path(ws)
        if "?" not in raw:
            return {}
        from urllib.parse import parse_qs, urlparse

        return {k: v[0] for k, v in parse_qs(urlparse(raw).query).items() if v}

    def _room_ok(self, query: dict[str, str], path: str, tid: str) -> tuple[bool, str]:
        """房间口令校验 → ``(放行?, 说明)``。

        ★ 实测（2026-10-04）：DG-LAB **App 扫了配对二维码之后只带 `tid`**，
          我们在 URL 里附加的 `?room=…` 被它丢掉了 —— 中继于是把手机**直接拒掉**
          （`logs/relay-audit.jsonl` 里连着 4 条 `bad_room`），面板上看到的就是
          "郊狼的 v4 socket 连不上"。

        所以口令认三种形态：
          1. ``?room=<口令>``           —— 我们自己那端（coyote_v4 节点）走这条；
          2. 路径段 ``/<口令>/…``       —— 官方 kit 说 V4 支持任意 path，多留一条路；
          3. **被控方（带 tid）不带口令但 tid 命中在线控制方** → 放行，但记审计
             （App 不给口令，硬拒就等于功能不能用；tid 只在配对二维码里出现过）。
        """
        if not self.room:
            return True, ""
        got = str(query.get("room") or "").strip()
        if got:
            return (got == self.room), ("ok" if got == self.room else "bad_room")
        segments = [s for s in str(path).split("?", 1)[0].split("/") if s]
        if any(s.strip() == self.room for s in segments):
            return True, "ok"
        if tid and tid in self.controllers:
            return True, "room_missing_accepted"
        return False, "room_missing"

    async def _handle(self, ws: Any) -> None:
        path = self._raw_path(ws)
        query = self._query_of(ws)
        tid = str(query.get("tid") or query.get("targetId") or "")

        # ★ 房间口令：给了就必须两端都带；被控方（App）不带时看 tid 是否命中在线控制方
        ok, why = self._room_ok(query, path, tid)
        if not ok:
            log.warning("拒绝连接：房间口令不对（path=%r 带着 room=%r，来自 %s）—— "
                        "要放宽/收紧请看 _room_ok 的注释",
                        path, query.get("room"), peer_of(ws))
            self._record(ws, "rejected", room_ok=False, reason=why, path=path)
            with contextlib.suppress(Exception):
                await ws.close(code=4403, reason="bad room")
            return
        if why == "room_missing_accepted":
            log.warning("被控方没有带房间口令（path=%r），但 tid=%s 命中了在线控制方 → 放行"
                        "（DG-LAB App 不保留我们附加的 query，见 _room_ok）来自 %s",
                        path, tid, peer_of(ws))

        client_id = secrets.token_hex(4)
        if tid:
            await self._as_client(ws, client_id, tid)
        else:
            await self._as_controller(ws, client_id)

    async def _as_controller(self, ws: Any, client_id: str) -> None:
        self.controllers[client_id] = ws
        self.label[ws] = f"控制方:{client_id}"
        rec = self._record(ws, "controller", room_ok=True, client_id=client_id)
        await self._send(ws, {"type": "hello", "clientId": client_id})
        log.info("控制方已连接 clientId=%s 来自 %s", client_id, rec.remote)
        try:
            async for raw in ws:
                await self._from_controller(ws, json.loads(raw))
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.controllers.pop(client_id, None)
            self._close_record(rec)
            log.info("控制方断开 clientId=%s（在线 %.1fs）", client_id, rec.seconds)

    async def _as_client(self, ws: Any, client_id: str, tid: str) -> None:
        controller = self.controllers.get(tid)
        await self._send(ws, {"type": "hello", "clientId": client_id})
        if controller is None:
            self._record(ws, "rejected", room_ok=True, client_id=client_id,
                         reason="controller_not_found")
            await self._send(ws, {"type": "error", "code": "controller_not_found"})
            await ws.close(code=4001)
            log.warning("被控方 %s 指定的控制方 %s 不存在（来自 %s）",
                        client_id, tid, peer_of(ws))
            return

        self.clients[ws] = controller
        self.client_ids[ws] = client_id
        self.label[ws] = f"被控方:{client_id}"
        rec = self._record(ws, "client", room_ok=True, client_id=client_id)
        await self._send(ws, {"type": "controller_attached", "clientId": tid})
        await self._send(controller, {"type": "client_attached", "clientId": client_id})
        log.info("被控方已接入 clientId=%s → 控制方 %s（来自 %s）", client_id, tid, rec.remote)

        try:
            async for raw in ws:
                await self._from_client(ws, json.loads(raw))
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.clients.pop(ws, None)
            self.client_ids.pop(ws, None)
            self._close_record(rec)
            if controller in self.label:
                await self._send(controller, {"type": "client_disconnected", "clientId": client_id})
            log.info("被控方断开 clientId=%s（在线 %.1fs）", client_id, rec.seconds)

    async def _from_controller(self, ws: Any, frame: dict) -> None:
        # 应用层心跳（v4-server.ts 的做法）：{"type":"ping"} → {"type":"pong"}
        if frame.get("type") == "ping":
            await self._send(ws, {"type": "pong", "ts": int(time.time() * 1000)})
            return
        if frame.get("type") in ("pong", "heartbeat"):
            return
        if frame.get("type") != "message":
            return
        target = frame.get("clientId")
        peer = None
        for cws, ctrl in self.clients.items():
            if ctrl is ws and self.client_ids.get(cws) == target:
                peer = cws
                break
        if peer is None:
            await self._send(ws, {"type": "error", "code": "client_not_found", "clientId": target})
            log.warning("目标被控方不存在 clientId=%s", target)
            return
        await self._send(peer, {"type": "message", "data": frame.get("data")})

    async def _from_client(self, ws: Any, frame: dict) -> None:
        if frame.get("type") == "ping":
            await self._send(ws, {"type": "pong", "ts": int(time.time() * 1000)})
            return
        if frame.get("type") in ("pong", "heartbeat"):
            return
        if frame.get("type") != "message":
            return
        controller = self.clients.get(ws)
        if controller is None:
            return
        await self._send(
            controller,
            {"type": "message", "clientId": self.client_ids.get(ws), "data": frame.get("data")},
        )

    @staticmethod
    async def _send(ws: Any, payload: dict) -> None:
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass


class FakeApp:
    """假被控方：连上 stub，报一台模拟郊狼，收到 ``device.op`` 就打印波形。"""

    def __init__(self, url: str, tid: str, on_pulse: Any = None) -> None:
        self.url = url
        self.tid = tid
        self.on_pulse = on_pulse
        self.fired: list[dict] = []
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run(self) -> None:
        import websockets

        sep = "&" if "?" in self.url else "?"
        while True:
            try:
                async with websockets.connect(f"{self.url}{sep}tid={self.tid}") as ws:
                    log.info("假 App 已接入（tid=%s）", self.tid)
                    # 接入后主动报设备（**必须包 message 帧**，见 _send_msg 注释）
                    await _send_msg(ws, {
                        "t": "ev", "ev": "devices.snapshot",
                        "devices": [{"slotId": SLOT_ID, "name": "模拟郊狼", "type": DEVICE_TYPE}],
                    })
                    async for raw in ws:
                        await self._on_message(ws, json.loads(raw))
            except Exception as exc:  # noqa: BLE001
                log.warning("假 App 连接结束（%s），1.5s 后重连", type(exc).__name__)
                await asyncio.sleep(1.5)

    async def _on_message(self, ws: Any, frame: dict) -> None:
        data = frame.get("data") or {}
        if isinstance(data, dict) and data.get("t") == "req":
            method = data.get("m")
            req_id = data.get("reqId")
            if method == "device.op":
                op = data.get("data") or {}
                frames = op.get("v") or []
                record = {
                    "channel": {0: "A", 1: "B"}.get(op.get("c"), str(op.get("c"))),
                    "type": op.get("t"),
                    "duration_ms": op.get("d"),
                    "frame_count": len(frames) if isinstance(frames, list) else 0,
                    "first_frame": (frames[0] if isinstance(frames, list) and frames else None),
                    "priority": op.get("p"),
                    "immediate": op.get("im"),
                }
                self.fired.append(record)
                log.info("🔥 假 App 收到开火：%s", record)
                if self.on_pulse is not None:
                    try:
                        self.on_pulse(record)
                    except Exception:  # noqa: BLE001
                        pass
                await _send_msg(ws, {"t": "resp", "reqId": req_id, "result": {}})
                return
            if method == "devices.get":
                await _send_msg(ws, {
                    "t": "resp", "reqId": req_id,
                    "result": {"devices": [
                        {"slotId": SLOT_ID, "name": "模拟郊狼", "type": DEVICE_TYPE}]},
                })
                return
            # 其它请求一律成功回应，别让 SDK 等超时
            await _send_msg(ws, {"t": "resp", "reqId": req_id, "result": {}})
