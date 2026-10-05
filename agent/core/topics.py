"""Zenoh key space —— **全项目唯一真源**。

来源：`docs/架构设计与路线图.md` §5.3（key space 表格）+ §5.4（节点清单）。

铁律（任务书 §4.1 / 附「绝对不要做」）：

    ❌ 在 topics.py 之外硬编码 key 字符串

其他任何文件（`agent/nodes/**`、`tests/**`、`agent/core/**`）都必须
`from agent.core import topics` 后调函数/常量取 key。
`tests/test_topics.py` 里有一条 AST 扫描测试专门守这条规矩。

§5.3 的表格在本文件里落成 `KEY_SPECS`（`KeySpec` 列表），
`CROSS_MACHINE_ALLOWLIST` / `ALL_SUFFIXES` / `is_cross_machine()` 全部**从它推导**，
不再手抄一遍 —— 这样"文档与代码一致"是可被测试断言的，而不是靠人眼核对。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "PREFIX",
    "DEFAULT_ROOM",
    "STATUS_PATTERN",
    "KeySpec",
    "KEY_SPECS",
    "ALL_SUFFIXES",
    "NODE_NAMES",
    "CROSS_MACHINE_ALLOWLIST",
    "FEATURE_FRAME_RAW",
    "FEATURE_POSE",
    "FEATURE_FACE",
    "FEATURE_AUDIO",
    "FEATURE_HEART",
    "EVENT_PAWPRINT",
    "EVENT_DEVICE",
    "EVENT_FIRE",
    "EVENT_LINK",
    "COMMAND_FIRE",
    "COMMAND_CLEAR",
    "COMMAND_SELECT_DEVICE",
    "COMMAND_RULES",
    "COMMAND_ROOM",
    "COMMAND_RAW_V4",
    "COMMAND_PULSE_RAW",
    "TEST_ECHO",
    "TEST_ECHO_OUT",
    "WILDCARD_ALL",
    "WILDCARD_ONE",
    "key",
    "feature",
    "command",
    "event",
    "status",
    "test",
    "all_keys",
    "feature_keys",
    "command_keys",
    "event_keys",
    "status_keys",
    "test_keys",
    "FEATURE_ALL",
    "EVENT_ALL",
    "COMMAND_ALL",
    "STATUS_ALL",
    "TEST_ALL",
    "cross_machine_keys",
    "is_cross_machine",
    "is_full_key",
    "room_of",
    "suffix_of",
    "resolve",
]

PREFIX = "coyotelink"
DEFAULT_ROOM = "local"

WILDCARD_ALL = "**"  # 匹配任意层级
WILDCARD_ONE = "*"  # 只匹配一层

#: 各前缀的"整棵子树"后缀 —— **订阅用**（room 由 `Node.subscribe` 补）。
#: 前端只需要这几类：feature / event / command / status / test。
#: ★ 特意**不含** `bridge/**`：那是跨机 AEAD 密文副本，面板用不上，
#:   捞进来只会让 web 节点白转发 + 刷"解不开"的 warning（实测面板 WS 每秒 132 条里
#:   ~70 条是这个），一忙就丢包 → 指令/状态随机收不到。
FEATURE_ALL = f"feature/{WILDCARD_ALL}"
EVENT_ALL = f"event/{WILDCARD_ALL}"
COMMAND_ALL = f"command/{WILDCARD_ALL}"
STATUS_ALL = f"status/{WILDCARD_ONE}"
TEST_ALL = f"test/{WILDCARD_ALL}"

# ──────────────────────────────────────────────────────────────
# 后缀常量（§5.3 表格左列；前缀 coyotelink/<room>/ 由函数拼）
# ──────────────────────────────────────────────────────────────
FEATURE_FRAME_RAW = "feature/frame.raw"  # ❌ 永不跨机
FEATURE_POSE = "feature/pose"
FEATURE_FACE = "feature/face"
FEATURE_AUDIO = "feature/audio"
FEATURE_HEART = "feature/heart"
EVENT_PAWPRINT = "event/pawprint"
EVENT_DEVICE = "event/device"
EVENT_FIRE = "event/fire"
#: ★ 链路状态（面板「🔌 连接与配对」卡片上那几盏灯）—— 把"连没连上"从日志搬进面板。
#: 发布者：`sim_coyote`（ESP32 串口 / 手机 App 在不在）、`coyote_v4`（Relay 通不通）、
#: `camera`（在用哪台 + **实测帧率**）、`heart`（状态 + **扫到的可用心率带列表**）。
#: 刻意**跨机**：对端也能在自己面板上看到链路状态。
EVENT_LINK = "event/link"
COMMAND_FIRE = "command/fire"
COMMAND_CLEAR = "command/clear"
COMMAND_SELECT_DEVICE = "command/select_device"
#: ★ 规则下发（web 面板 → rules 判定引擎）—— 面板上配的规则不再只存 localStorage
COMMAND_RULES = "command/rules"
#: ★ 换房间（web 面板 → **所有节点**）—— 房间就是 key 前缀 `coyotelink/<room>/…`，
#: 所以"换房间"必须全体跟着重声明 pub/sub，不是 web 一家的事（见 `Node.switch_room`）。
#: payload：`{"action": "create"|"join"|"leave", "code": "AB12CD", "url": "ws://…"}`。
#: 刻意**跨机**：对面那台机器的节点也得换到同一个房间，才算"在一个房间里"。
COMMAND_ROOM = "command/room"
COMMAND_RAW_V4 = "command/raw_v4"  # ❌ 仅本机
#: ★ 模拟主机截获的 App **原始脉冲流**（每 100ms 一条）—— 仅本机，由 sim_coyote 发。
#: 与 `COMMAND_RAW_V4` 的区别：`raw_v4` 是「喂给 SDK 的入站帧」（对端 → SDK），
#: 这条是「App 的原始意图」（B0 的强度变化 + 8 字节波形帧），由 coyote_v4 转成 `device.op` 发出。
COMMAND_PULSE_RAW = "command/pulse_raw"  # ❌ 仅本机

STATUS_PATTERN = "status/<node>"  # 每节点一条心跳，key 里带节点名
#: `status/` 前缀（从上面的模式推导，保持单一真源）。
#: 消费方判前缀时用这个 —— 别自己写 `'status/'` 字面量（会踩 F9 的"无裸 key"检查）。
STATUS_PREFIX = STATUS_PATTERN.split("<", 1)[0]

# 测试专用命名空间（F1 的 EchoNode、F2 的 cli sub/pub 都用它）
TEST_ECHO = "test/echo"
TEST_ECHO_OUT = "test/echo.out"


@dataclass(frozen=True)
class KeySpec:
    """§5.3 表格的一行。`cross_machine` 就是表里那个 ✅/❌。"""

    suffix: str
    publishers: tuple[str, ...]
    subscribers: tuple[str, ...]
    cross_machine: bool
    note: str = ""


#: §5.4 的 12 个节点名（`agent/nodes.toml` 与 cli 据此校验/展示）
#: 放在 KEY_SPECS 之前：`command/room` 的订阅者就是"所有节点"。
NODE_NAMES: tuple[str, ...] = (
    "camera",
    "pose",
    "face",
    "audio",
    "device",
    "coyote_v4",
    "sim_coyote",
    "pawprints",
    "heart",
    "rules",
    "bridge",
    "web",
)


# ★ §5.3 表格的逐行落地 —— 改这里必须同步改文档（反之亦然）
KEY_SPECS: tuple[KeySpec, ...] = (
    KeySpec(
        FEATURE_FRAME_RAW,
        ("camera",),
        ("pose", "face"),
        False,
        "绝不外发：需求 1d『不传视频』的代码级落地",
    ),
    KeySpec(FEATURE_POSE, ("pose",), ("web", "rules", "bridge"), True),
    KeySpec(FEATURE_FACE, ("face",), ("web", "rules", "bridge"), True),
    KeySpec(FEATURE_AUDIO, ("audio",), ("web", "rules", "bridge"), True),
    KeySpec(FEATURE_HEART, ("heart",), ("web", "rules", "bridge"), True),
    KeySpec(EVENT_PAWPRINT, ("pawprints",), ("rules", "web", "bridge"), True),
    KeySpec(EVENT_DEVICE, ("device",), ("web", "bridge"), True),
    KeySpec(EVENT_FIRE, ("coyote_v4",), ("web", "bridge"), True),
    KeySpec(
        EVENT_LINK,
        ("sim_coyote", "coyote_v4", "camera", "heart"),
        ("web", "bridge"),
        True,
        "链路状态：Relay 通不通 / ESP32 串口有没有数据 / 手机 App 在不在 / 摄像头开没开",
    ),
    KeySpec(COMMAND_FIRE, ("rules", "web"), ("coyote_v4",), True),
    KeySpec(COMMAND_CLEAR, ("rules", "web"), ("coyote_v4",), True),
    KeySpec(
        COMMAND_RULES,
        ("web",),
        ("rules",),
        True,
        "规则下发：面板上配好的规则推给判定引擎（替换语义，见 RulesNode.set_rules）",
    ),
    KeySpec(
        COMMAND_ROOM,
        ("web",),
        NODE_NAMES,
        True,
        "换房间：房间 = key 前缀，**各节点**都在基类里订阅它并重声明 pub/sub（Node.switch_room）",
    ),
    KeySpec(
        COMMAND_SELECT_DEVICE,
        ("web",),
        ("device", "camera", "audio", "sim_coyote", "heart", "pawprints"),
        True,
        "设备热切换（M0 通用设备层）—— BLE 设备（假主机/心率带/爪印）也在内",
    ),
    KeySpec(COMMAND_RAW_V4, ("coyote_v4",), ("coyote_v4",), False,
            "仅本机；手动传输模式：SDK 出站帧与入站帧共用（靠内容去重破自环）"),
    KeySpec(
        COMMAND_PULSE_RAW,
        ("sim_coyote",),
        ("coyote_v4",),
        False,
        "仅本机：模拟主机截获的 App 原始脉冲流（B0 → 强度 + 8 字节波形帧）",
    ),
    KeySpec(
        STATUS_PATTERN,
        ("各节点",),
        ("supervisor", "web"),
        False,
        "可选跨机；心跳内容 = ts/name/room/pid/uptime_s",
    ),
)

#: **消息**后缀（不含 status/<node> 这个带占位符的模式）
ALL_SUFFIXES: tuple[str, ...] = tuple(s.suffix for s in KEY_SPECS if s.suffix != STATUS_PATTERN)

#: ★ 跨机白名单（bridge 节点据此过滤）= 从 KEY_SPECS 推导，不手抄
#: `FEATURE_FRAME_RAW`、`COMMAND_RAW_V4`、`COMMAND_PULSE_RAW` **都不在里面** ——
#: 这是需求 1d「不传视频 / 原始脉冲」的代码级落地。
CROSS_MACHINE_ALLOWLIST: tuple[str, ...] = tuple(s.suffix for s in KEY_SPECS if s.cross_machine)


# ──────────────────────────────────────────────────────────────
# 拼接函数
# ──────────────────────────────────────────────────────────────
def _check_suffix(suffix: str) -> str:
    if not isinstance(suffix, str) or not suffix:
        raise ValueError(f"key 后缀必须是非空字符串，得到 {suffix!r}")
    if suffix.startswith("/") or suffix.endswith("/"):
        raise ValueError(f"key 后缀不能以 '/' 开头或结尾：{suffix!r}")
    if "//" in suffix:
        raise ValueError(f"key 后缀不能含空层级 '//'：{suffix!r}")
    if any(ch.isspace() for ch in suffix):
        raise ValueError(f"key 后缀不能含空白字符：{suffix!r}")
    return suffix


def _k(room: str, suffix: str) -> str:
    """内部：`coyotelink/<room>/<suffix>`。"""
    _check_suffix(suffix)
    if not isinstance(room, str) or not room or "/" in room or any(c.isspace() for c in room):
        raise ValueError(f"room 必须是不含 '/' 和空白的非空字符串，得到 {room!r}")
    return f"{PREFIX}/{room}/{suffix}"


def key(suffix: str, room: str = DEFAULT_ROOM) -> str:
    """任意后缀 → 完整 key（万能入口）。"""
    return _k(room, suffix)


def feature(topic: str, room: str = DEFAULT_ROOM) -> str:
    """`feature/*` 主题（topic 传 `FEATURE_POSE` 这类常量，或 `'pose'`）。"""
    return _k(room, topic if topic.startswith("feature/") else f"feature/{topic}")


def command(topic: str, room: str = DEFAULT_ROOM) -> str:
    """`command/*` 主题。"""
    return _k(room, topic if topic.startswith("command/") else f"command/{topic}")


def event(topic: str, room: str = DEFAULT_ROOM) -> str:
    """`event/*` 主题。"""
    return _k(room, topic if topic.startswith("event/") else f"event/{topic}")


def status(node: str, room: str = DEFAULT_ROOM) -> str:
    """`status/<node>` —— 节点心跳。"""
    _check_suffix(node)
    return _k(room, f"status/{node}")


def test(topic: str, room: str = DEFAULT_ROOM) -> str:
    """`test/*` —— 只用于测试/调试，不参与业务。"""
    return _k(room, topic if topic.startswith("test/") else f"test/{topic}")


# ──────────────────────────────────────────────────────────────
# 通配符模式（订阅用）
# ──────────────────────────────────────────────────────────────
def all_keys(room: str = DEFAULT_ROOM) -> str:
    """本房间全部 key：`coyotelink/<room>/**`。"""
    return _k(room, WILDCARD_ALL)


def feature_keys(room: str = DEFAULT_ROOM) -> str:
    return _k(room, f"feature/{WILDCARD_ALL}")


def command_keys(room: str = DEFAULT_ROOM) -> str:
    return _k(room, f"command/{WILDCARD_ALL}")


def event_keys(room: str = DEFAULT_ROOM) -> str:
    return _k(room, f"event/{WILDCARD_ALL}")


def status_keys(room: str = DEFAULT_ROOM) -> str:
    """`coyotelink/<room>/status/*` —— 单层通配，不含子层级。"""
    return _k(room, f"status/{WILDCARD_ONE}")


def test_keys(room: str = DEFAULT_ROOM) -> str:
    """`coyotelink/<room>/test/**` —— F2/F3 用。"""
    return _k(room, f"test/{WILDCARD_ALL}")


def cross_machine_keys(room: str = DEFAULT_ROOM) -> tuple[str, ...]:
    """白名单里的 key 的具体形态（bridge 订阅用）。"""
    return tuple(_k(room, suffix) for suffix in CROSS_MACHINE_ALLOWLIST)


# ──────────────────────────────────────────────────────────────
# 解析与判定
# ──────────────────────────────────────────────────────────────
def is_full_key(value: str) -> bool:
    """是否已经是完整 key（而不是后缀）。"""
    return isinstance(value, str) and value.startswith(f"{PREFIX}/")


def resolve(value: str, room: str = DEFAULT_ROOM) -> str:
    """后缀 → 补全；已经是完整 key → 原样返回。

    让 `publish("feature/pose")` 与 `publish("coyotelink/local/feature/pose")`
    都能工作，CLI 手敲 key 时很方便。
    """
    if is_full_key(value):
        return value
    return _k(room, value)


def suffix_of(full_key: str) -> str:
    """完整 key → 去掉 `coyotelink/<room>/` 后的后缀。"""
    if not is_full_key(full_key):
        raise ValueError(f"不是完整 key（应以 '{PREFIX}/' 开头）：{full_key!r}")
    parts = full_key.split("/", 2)
    if len(parts) < 3 or not parts[1]:
        raise ValueError(f"key 缺少 room 层级：{full_key!r}")
    return parts[2]


def room_of(full_key: str) -> str:
    """完整 key → room。"""
    if not is_full_key(full_key):
        raise ValueError(f"不是完整 key（应以 '{PREFIX}/' 开头）：{full_key!r}")
    parts = full_key.split("/", 2)
    if len(parts) < 2 or not parts[1]:
        raise ValueError(f"key 缺少 room 层级：{full_key!r}")
    return parts[1]


def is_cross_machine(value: str) -> bool:
    """这个 key（或后缀）允许跨机吗？

    输入可以是完整 key 或后缀；`status/pose` 这类不在白名单里 → False。
    """
    suffix = suffix_of(value) if is_full_key(value) else value
    return suffix in CROSS_MACHINE_ALLOWLIST
