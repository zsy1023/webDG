"""T3 实测探针：验证 Zenoh 跨机配置键，并做「不加白名单时原始帧会跨机」的对照实验。

**这是 T3 的实测证据生成器**（任务书要求：不要猜键名，跑起来验证）。

用法（cwd = 工作区根）::

    python agent/nodes/bridge/_probe_zenoh.py

它会起两个**独立进程**模拟 A/B 两端：

* A 端（listener）：`listen/endpoints = ["tcp/127.0.0.1:7455"]`
* B 端（sender）  ：`connect/endpoints = ["tcp/127.0.0.1:7455"]`

B 端发两条消息：

* `coyotelink/local/feature/pose`      —— 白名单内
* `coyotelink/local/feature/frame.raw` —— **白名单外（隐私铁律）**

A 端打印它收到了哪些 key。结论有两个：

1. **键名正确**：能收到，说明 `listen/endpoints` / `connect/endpoints` 是对的
   （键名写错会一条都收不到，甚至 `insert_json5` 报错）。
2. **直连会泄漏**：A 端**同时**收到了 `feature/frame.raw` —— 证明"只要两端 Zenoh
   直接连通，所有 key 都会过去"。所以隐私铁律**必须由 bridge 的白名单在代码里强制**，
   而不能靠 Zenoh 配置本身。

（bridge 的端到端白名单验证见 `selftest.py`。）
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

import zenoh

# 本机 loopback 上换个不常用端口，避免和默认 7447 / 项目其它实例冲突
PORT = "127.0.0.1:7455"
LISTEN_ENDPOINT = f"tcp/{PORT}"

KEY_ALLOWLISTED = "coyotelink/local/feature/pose"
KEY_FORBIDDEN = "coyotelink/local/feature/frame.raw"


def _run_listener() -> None:
    cfg = zenoh.Config()
    # ★ 被测键名 1：listen/endpoints
    cfg.insert_json5("listen/endpoints", json.dumps([LISTEN_ENDPOINT]))
    # 关 multicast：否则两端靠自动发现就能通，键名写错也测不出来
    cfg.insert_json5("scouting/multicast/enabled", "false")
    session = zenoh.open(cfg)
    got: list[str] = []
    sub = session.declare_subscriber("coyotelink/local/feature/**",
                                     lambda s: got.append(str(s.key_expr)))
    time.sleep(4.0)
    sub.undeclare()
    session.close()
    print("LISTENER_KEYS=" + json.dumps(got), flush=True)


def _run_sender() -> None:
    time.sleep(1.5)  # 等 listener 把端口监听起来
    cfg = zenoh.Config()
    # ★ 被测键名 2：connect/endpoints
    cfg.insert_json5("connect/endpoints", json.dumps([LISTEN_ENDPOINT]))
    cfg.insert_json5("scouting/multicast/enabled", "false")
    session = zenoh.open(cfg)
    pub_a = session.declare_publisher(KEY_ALLOWLISTED)
    pub_b = session.declare_publisher(KEY_FORBIDDEN)
    for i in range(3):
        pub_a.put(json.dumps({"seq": i, "present": True}).encode("utf-8"))
        pub_b.put(b"\x00\x01\x02 RAW FRAME (must stay local) \x03")
        time.sleep(0.4)
    time.sleep(1.0)
    session.close()
    print("SENDER_DONE", flush=True)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "listener":
        _run_listener()
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "sender":
        _run_sender()
        return 0

    # 父进程：起两个独立子进程
    print(f"[probe] python={sys.executable}", flush=True)
    print(f"[probe] listen={LISTEN_ENDPOINT}", flush=True)
    listener = subprocess.Popen([sys.executable, __file__, "listener"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    sender = subprocess.Popen([sys.executable, __file__, "sender"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out_s = sender.communicate(timeout=30)[0]
    out_l = listener.communicate(timeout=30)[0]
    print("---- sender ----")
    print(out_s)
    print("---- listener ----")
    print(out_l)

    received = []
    for line in out_l.splitlines():
        if line.startswith("LISTENER_KEYS="):
            received = json.loads(line.split("=", 1)[1])
    print("[probe] listener received keys:", received)
    print("[probe] allowlisted passed :", KEY_ALLOWLISTED in received)
    print("[probe] forbidden  passed  :", KEY_FORBIDDEN in received)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
