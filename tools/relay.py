"""对端 V4 中继 —— **被控端跑这个**，控制端的 ``coyote_v4`` 直连它。

    python tools/relay.py --host 0.0.0.0 --port 9997     # 会自动生成房间口令并打印
    python tools/relay.py --port 9997 --room AB12CD     # 也可以自己指定

**房间口令**：两端连接都必须带 ``?room=<口令>``，否则拒连（close 4403）——
穿透到公网时别再裸奔。控制端写在 ``[coyote_v4] url`` 里，
手机 App 的配对二维码由 ``tools/dev/coyote_lab.py --room …`` 自动带上。
（它是明文 query，能挡陌生人，但**不等于端到端加密**。）

拓扑::

    被控者手机 DG-LAB App ──扫码──┐
                                  ├──> 本中继 (ws://<被控端>:9997) ──> 真郊狼
    控制端 coyote_v4 ─────────────┘
        （agent/config.toml: [coyote_v4] url = "ws://<被控端>:9997"）

中继做「1 控制方 : 1 被控方」的帧透传：控制方连上先拿到 ``targetId``，
被控者手机 App 用**带该 tid 的配对二维码**扫码接入即可配对。

> 这是 T4「直连对端 relay」的部署形状（协调者 2026-10-04 定）。
> 走公网时外面套樱花穿透；要加密就在 bridge 层开 AEAD，或给中继套 TLS。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.core.config import force_utf8  # noqa: E402
from agent.core.logging import style  # noqa: E402
from tools.dev.v4_stub import RelayStub  # noqa: E402

async def main() -> int:
    ap = argparse.ArgumentParser(description="CoyoteLink 对端 V4 中继（被控端运行）")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0，便于对端连入）")
    ap.add_argument("--port", type=int, default=9997)
    ap.add_argument("--room", default=None,
                    help="房间口令（两端连接都要带 ?room=…）。不给就自动生成一个并打印")
    args = ap.parse_args()

    # 中继自己那几条日志也走统一 logger —— 这样终端里同样是彩色的（措辞/级别一致）
    from agent.core.logging import setup_root_logging

    setup_root_logging(level="INFO")
    force_utf8()
    room = (args.room or "").strip() or secrets.token_hex(3).upper()
    stub = RelayStub(host=args.host, port=args.port, room=room)

    # ── 接入审计（需求 7「看到是谁访问了」）────────────────────
    audit_path = ROOT / "logs" / "relay-audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    _ROLE_LABEL = {"controller": "控制方", "client": "被控方", "rejected": "拒绝"}

    def _on_access(rec) -> None:  # noqa: ANN001
        d = rec.to_dict()
        label = _ROLE_LABEL.get(rec.role, rec.role)
        # 一行一个事件，颜色直接说明好坏：拒绝=红、断开/异常=黄、正常接入=绿
        if rec.role == "rejected" or not rec.room_ok:
            tone, label = "red", style(label, "bold_red")
        elif rec.reason:
            tone, label = "yellow", style(label, "yellow")
        else:
            tone, label = "green", style(label, "bold_green")
        log_style = {"red": "red", "yellow": "yellow", "green": "green"}[tone]
        head = (
            f"  {style(d['at_iso'], 'gray')} {label} "
            f"{style('接入', log_style)} {style(rec.remote, 'cyan')}"
        )
        if not rec.room_ok:
            head += style("（口令不符）", "bold_red")
        if rec.client_id:
            head += f"  {style('clientId=' + str(rec.client_id), 'gray')}"
        if rec.reason:
            head += f"  {style(rec.reason, log_style)}"
        if rec.path and (rec.role == "rejected" or not rec.room_ok):
            # ★ 被拒时把**原始 URL** 打出来：这是排"口令为什么不对"的唯一线索
            #   （实测手机 App 就是靠这行才看清它根本没带 ?room=）
            head += f"\n      {style('URL: ' + rec.path, 'gray')}"
        print(head, flush=True)
        try:
            with audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(d, ensure_ascii=False) + "\n")
        except OSError:
            pass

    stub.on_access = _on_access

    await stub.start()
    banner = style("═" * 68, "gold")
    print(banner, flush=True)
    print(f" {style('V4 中继已启动', 'bold_gold')}：ws://{args.host}:{args.port}", flush=True)
    print(f" ★ 房间口令：{style(room, 'bold_cyan')}", flush=True)
    print("   控制端 agent/config.toml → [coyote_v4]")
    print(f"        url = {style(f'ws://<本机IP或公网地址>:{args.port}?room={room}', 'cyan')}")
    print("   手机 App：扫控制端打印的配对二维码（码里已带 room，不用手填）")
    print(f" 接入审计：实时打印在这里，同时追加写 {style(str(audit_path), 'gray')}")
    print(banner, flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await stub.stop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
