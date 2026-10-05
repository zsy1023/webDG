"""T3 同机双进程**端到端实测** —— 白名单的最终证据生成器。

它用一台机器上**四个独立进程**模拟"两台 NAT 后的机器"：

::

    ┌── A 侧（本机网络 7461）──┐          ┌── B 侧（本机网络 7462）──┐
    │  localA(pub/sub)  │       │  跨机链路 │       │  localB(sub)      │
    │        ▲          ▼       │  7451     │       ▼                   │
    │      bridge A ════════════╪══════════►│   bridge B                │
    └───────────────────────────┘           └───────────────────────────┘

* A 侧 localA 往**本机网络**发两条：``feature/pose``（白名单内）与
  ``feature/frame.raw``（**白名单外 / 隐私铁律**）；
* bridge A 只订阅白名单 key → 只有 ``feature/pose`` 上了跨机链路；
* B 侧 localB 监听本机网络收结果。

**判据**：localB 收到 ``feature/pose``（说明桥通了），且**收不到** ``feature/frame.raw``
（说明隐私铁律由代码强制）。两条同时成立才算 PASS。

为什么必须显式关 multicast：同机四个 session 若都开 multicast 会**自动合并成一个网络**，
"A 的本地注入"会被"B 的本地订阅"直接看到 —— 那就没法模拟"两台隔离的机器"了。
所以所有 session 都 ``scouting/multicast/enabled = false``，只用显式 endpoint 互联。

用法（cwd = 工作区根）::

    python agent/nodes/bridge/selftest.py            # 编排 4 个进程并判定
    python agent/nodes/bridge/selftest.py _local ... # 内部子命令，手别碰
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import zenoh

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.core import topics  # noqa: E402
from agent.nodes.bridge import room  # noqa: E402

LOCAL_ROOM = "local"
SUFFIX_ALLOWED = topics.FEATURE_POSE          # 白名单内
SUFFIX_FORBIDDEN = topics.FEATURE_FRAME_RAW   # 白名单外（永不跨机）


# ──────────────────────────────────────────────────────────────
# 子命令：一个"本机节点"（只连本侧本地网络）
# ──────────────────────────────────────────────────────────────
def _local_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="selftest _local")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--mode", choices=["pub", "sub"], required=True)
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--publish", default="")  # 逗号分隔的 suffix
    parser.add_argument("--room", default=LOCAL_ROOM)
    args = parser.parse_args(argv)

    cfg = zenoh.Config()
    cfg.insert_json5("connect/endpoints", json.dumps([args.endpoint]))
    cfg.insert_json5("scouting/multicast/enabled", "false")
    session = zenoh.open(cfg)

    seen: list[str] = []

    def on_sample(sample: object) -> None:
        seen.append(str(getattr(sample, "key_expr", "?")))

    sub = session.declare_subscriber(topics.key("feature/**", args.room), on_sample)

    if args.mode == "pub":
        suffixes = [s for s in args.publish.split(",") if s]
        deadline = time.time() + args.seconds
        while time.time() < deadline:
            for suffix in suffixes:
                session.put(topics.key(suffix, args.room), b"\x00payload\xff")
            time.sleep(0.4)
        print("PUB_DONE", flush=True)
    else:
        time.sleep(args.seconds)
        print("SUB_KEYS=" + json.dumps(seen), flush=True)

    sub.undeclare()
    session.close()
    return 0


# ──────────────────────────────────────────────────────────────
# 编排
# ──────────────────────────────────────────────────────────────
def _free_port(rng: random.Random) -> int:
    return rng.randint(20000, 40000)


def _write_bridge_config(
    path: Path,
    *,
    local_listen: int,
    bridge: dict[str, object],
) -> None:
    """写一个给 bridge 子进程用的 config.toml（本地 session + 跨机 session 都关 multicast）。"""
    lines = ['room = "local"', "", "[zenoh]", f'listen = ["tcp/127.0.0.1:{local_listen}"]', ""]
    lines += ["[zenoh.raw_json5]", '"scouting/multicast/enabled" = "false"', ""]
    lines += ["[bridge]"]
    for key, value in bridge.items():
        if isinstance(value, list):
            rendered = "[" + ", ".join(f'"{v}"' for v in value) + "]"
            lines.append(f"{key} = {rendered}")
        elif isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        else:
            lines.append(f'{key} = "{value}"')
    lines += ["", "[bridge.raw_json5]", '"scouting/multicast/enabled" = "false"', ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def _spawn(argv: list[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        argv,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def orchestrate() -> int:
    rng = random.Random()
    port_a_local = _free_port(rng)
    port_b_local = _free_port(rng)
    port_link = _free_port(rng)
    code = room.generate_code()

    tmp = Path(tempfile.mkdtemp(prefix="bridge-selftest-"))
    cfg_a = tmp / "bridge_a.toml"
    cfg_b = tmp / "bridge_b.toml"
    _write_bridge_config(
        cfg_a,
        local_listen=port_a_local,
        bridge={"role": "a", "room_code": code, "listen": [f"tcp/127.0.0.1:{port_link}"]},
    )
    _write_bridge_config(
        cfg_b,
        local_listen=port_b_local,
        bridge={"role": "b", "room_code": code, "connect": [f"tcp/127.0.0.1:{port_link}"]},
    )

    print(f"[selftest] 房间码={code}  link={port_link}  A-local={port_a_local}  B-local={port_b_local}")

    procs: list[subprocess.Popen[str]] = []
    try:
        bridge_a = _spawn([sys.executable, "-m", "agent.nodes.bridge", "--config", str(cfg_a)])
        bridge_b = _spawn([sys.executable, "-m", "agent.nodes.bridge", "--config", str(cfg_b)])
        procs += [bridge_a, bridge_b]
        time.sleep(3.0)  # 等两个 session 建链

        sub_b = _spawn([sys.executable, __file__, "_local",
                        "--endpoint", f"tcp/127.0.0.1:{port_b_local}",
                        "--mode", "sub", "--seconds", "12"])
        procs.append(sub_b)
        time.sleep(1.5)  # 让 B 侧订阅就位

        pub_a = _spawn([sys.executable, __file__, "_local",
                        "--endpoint", f"tcp/127.0.0.1:{port_a_local}",
                        "--mode", "pub", "--seconds", "7",
                        "--publish", f"{SUFFIX_ALLOWED},{SUFFIX_FORBIDDEN}"])
        procs.append(pub_a)

        out_a = pub_a.communicate(timeout=40)[0]
        out_b = sub_b.communicate(timeout=40)[0]
        print("---- A(local pub) ----")
        print(out_a)
        print("---- B(local sub) ----")
        print(out_b)

        received: list[str] = []
        for line in out_b.splitlines():
            if line.startswith("SUB_KEYS="):
                received = json.loads(line.split("=", 1)[1])

        allowed_key = topics.key(SUFFIX_ALLOWED, LOCAL_ROOM)
        forbidden_key = topics.key(SUFFIX_FORBIDDEN, LOCAL_ROOM)
        ok_allowed = allowed_key in received
        ok_forbidden_absent = forbidden_key not in received

        print(f"[selftest] B 侧收到的 key: {received}")
        print(f"[selftest] 白名单 key 过河 ({allowed_key}): {ok_allowed}")
        print(f"[selftest] 原始帧被拦 ({forbidden_key} 未到): {ok_forbidden_absent}")

        if ok_allowed and ok_forbidden_absent:
            print("RESULT=PASS")
            return 0
        print("RESULT=FAIL")
        return 1
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        time.sleep(0.5)
        for proc in procs:
            if proc.poll() is None:
                proc.kill()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "_local":
        return _local_main(argv[1:])
    return orchestrate()


if __name__ == "__main__":
    raise SystemExit(main())
