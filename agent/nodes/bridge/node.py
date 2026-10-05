"""``bridge`` 节点 —— 跨机白名单桥接（T3）。

它把本机 Zenoh 网络与"对端机器"的 Zenoh 网络**只在白名单 key 上**打通。

设计要点
--------

**为什么要一个自研 bridge，而不是让两端 Zenoh 直接连？**
直接 ``connect`` 会让两端**全连通** —— ``feature/frame.raw`` 也会过去，
违反需求 1d「不传视频」。Zenoh 本身没有"按 key 白名单"的 peer 级 ACL，
所以隐私铁律必须由本节点在**代码层**强制。

**两个网络、两个 session**

* ``self.session``（基类管的"本机 session"）：与**本机的其他节点**通信；
* ``self._remote``（本节点自开的"跨机 session"）：只连**对端 bridge**。

两者互不相通，唯一的"桥"就是本节点的转发逻辑 —— 于是白名单在这里是**唯一闸门**。

**链路 key 布局**（只存在于跨机网络，本机节点谁也不订阅它）::

    coyotelink/<link_room>/bridge/<from_role>/<suffix>

``<from_role>`` 是**发送方**角色（``a``/``b``）：a 端只发 ``bridge/a/...``、
只订 ``bridge/b/...``，b 端相反 —— 天然消除"自己发的被自己收到"。

**白名单是硬约束（两道闸门）**

1. **本机侧只订阅白名单 key**（:data:`agent.core.topics.CROSS_MACHINE_ALLOWLIST`）：
   ``feature/frame.raw`` 与 ``command/raw_v4`` **连进入本进程的订阅都没有**；
2. **链路侧再复核一遍**：收到任何链路 key 都过 :func:`agent.core.topics.is_cross_machine`，
   非白名单**一律丢弃并告警**（防对端/攻击者往链路里塞原始帧）。

**本机自环的破除**：Zenoh 默认会把"本 session 的发布"回送给"本 session 的订阅"
（self/loopback）。bridge 往本机注入对端数据时，会**被自己的本机订阅收到**，
若不处理就会 a→b→a 无限回环。这里用**进程内注入去重表**（精确匹配
``(suffix, payload_bytes)``，限量裁剪）把"自己刚注入的那条"识别出来并跳过 ——
确定、可单测，不依赖任何未验证的 Zenoh 特性。

**可选 E2E 加密**：见 :mod:`agent.nodes.bridge.crypto`（``encrypt=True`` 时启用）。

**中继（NAT 双向打洞失败时）**：两端都 ``connect`` 到同一台 ``zenohd``（router 模式），
中继只转发密文，看不到明文。见同目录 ``README.md``。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import time
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import zenoh

from agent.core import Node, topics
from agent.core.config import Config, force_utf8, normalize_level
from agent.core.node import run_node

from . import crypto, room

__all__ = ["BridgeNode", "NODE", "LINK_LAYER", "HELLO_SUFFIX"]

#: 跨机链路的中间层标记：``coyotelink/<link_room>/bridge/<role>/<suffix>``
LINK_LAYER = "bridge"
#: 握手（角色协商 / 心跳）用的伪后缀；它不是业务 key，会走白名单之外的专门分支
HELLO_SUFFIX = "_hello"

#: 注入去重表的容量上限（超过就丢最旧的记项）
_INJECTED_CAP = 4096
#: 默认 hello 周期（秒）
DEFAULT_HELLO_INTERVAL = 5.0


def _as_list(value: Any) -> list[str]:
    """把 ``str`` / ``list`` / ``None`` 规整成 ``list[str]``。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


#: "像不像一个随机房间码"：6~16 位大写字母数字。
#: ⚠️ 刻意比 `room.normalize_code()`（6 位 + 31 字符表，连 `L` 都不许）**宽松** ——
#: 面板那边生成的是 `[A-Z0-9]{4,16}`（可能含 `L`、长度也不一定是 6），
#: 拿严格校验去卡它会把合法房间码判成"不像房间码"。
_ROOM_CODE_SHAPE = re.compile(r"^[A-Z0-9]{6,16}$")


def _looks_like_room_code(value: str) -> bool:
    """粗略判断"这看起来是个随机房间码"（只用于**决定要不要自动加密** + 报警告）。

    判断不了熵，只能看形状：全大写字母数字、6~16 位。`local` / `bedroom` 这种
    人起的名字会被判成"弱"。
    """
    return bool(_ROOM_CODE_SHAPE.match(str(value or "")))


class BridgeNode(Node):
    """白名单跨机桥接节点。

    :param config: 节点配置（``Config``）；``None`` 走默认加载
    :param role: 本端角色 ``"a"`` / ``"b"``（谁建房谁是 a）
    :param link_room: 链路房间码（跨机 key 的 room 层）；缺省用配置里的 ``room_code`` 或本机 room
    :param listen: 跨机监听 endpoint 列表（如 ``["tcp/0.0.0.0:7451"]``）
    :param connect: 跨机连接 endpoint 列表（如 ``["tcp/<对端>:7451"]``）
    :param encrypt: 是否启用应用层 AEAD（``None`` = 用配置）
    :param remote_json5: 透传到跨机 session 的原生 json5 配置（如关 multicast）
    :param remote_session: **测试用**逃生舱：注入一个假 session，跳过 ``zenoh.open``
    """

    name = "bridge"

    def __init__(
        self,
        config: Config | None = None,
        *,
        role: str | None = None,
        link_room: str | None = None,
        listen: Iterable[str] | str | None = None,
        connect: Iterable[str] | str | None = None,
        encrypt: bool | None = None,
        remote_json5: dict[str, Any] | None = None,
        remote_session: Any | None = None,
    ) -> None:
        super().__init__(config)

        cfg = (self.config.raw or {}).get("bridge") or {}
        if not isinstance(cfg, dict):
            raise ValueError(f"[bridge] 必须是表（table），得到 {type(cfg).__name__}")

        self.role = room.validate_role(role if role is not None else cfg.get("role") or "a")
        self.peer_role = room.peer_role(self.role)
        #: 链路房间（跨机那套 key 的 room）。显式配了 `[bridge] room_code` → **钉死**，
        #: 跟业务房间各走各的；没配 → 就跟着业务房间走（换房间时它也跟着换，见 on_room_switched）。
        explicit_link_room = link_room if link_room is not None else cfg.get("room_code")
        self.link_room: str = str(explicit_link_room or self.room)
        self._link_room_follows_room = not explicit_link_room
        self._listen = _as_list(listen if listen is not None else cfg.get("listen"))
        self._connect = _as_list(connect if connect is not None else cfg.get("connect"))
        #: 显式要求加密与否；`None` = 没配 → 按"有没有真房间码"自动决定（见 `_refresh_key`）
        _requested = encrypt if encrypt is not None else cfg.get("encrypt")
        self._encrypt_requested: bool | None = None if _requested is None else bool(_requested)
        self.encrypt = False
        self._key: bytes | None = None
        self._remote_json5: dict[str, Any] = dict(remote_json5 or cfg.get("raw_json5") or {})
        self.hello_interval = float(cfg.get("hello_interval_s", DEFAULT_HELLO_INTERVAL))
        self._refresh_key()          # 密钥 = 房间码经 HKDF 派生（"一个房间码搞定房间 + 链路"）

        # 跨机 session（测试可注入假 session）
        self._remote: Any = remote_session
        self._owned_remote = remote_session is None

        # 订阅 / 发布句柄
        self._local_subs: list[Any] = []
        self._remote_sub: Any = None
        self._hello_pub: Any = None
        self._remote_pubs: dict[str, Any] = {}
        self._local_pubs: dict[str, Any] = {}

        # 注入去重表：{(suffix, payload_bytes): 剩余次数}
        self._injected: dict[tuple[str, bytes], int] = {}
        self._injected_order: deque[tuple[str, bytes]] = deque()

        # 对端信息 / 回调（供"看到谁访问了"）
        self.peer_info: dict[str, Any] | None = None
        self.on_peer_state: Callable[[str], None] | None = None
        self._last_hello: float = 0.0
        self._peer_connected = False
        self._hello_task: asyncio.Task[Any] | None = None

        # 统计（诊断用）
        self.stats = {"out": 0, "in": 0, "dropped": 0, "loop_skipped": 0}

    # ──────────────────────────────────────────────────────────────
    # 生命周期
    # ──────────────────────────────────────────────────────────────
    async def setup(self) -> None:
        # ★ 顺序很重要：**先开远端 session，再订阅本地房间** ⚠️ 实测坑
        #   反过来的话会有竞态：本地订阅一挂上，同机其它节点立刻在发消息，
        #   回调 `forward_to_remote` 进来时 `self._remote` 还是 None →
        #   `'NoneType' object has no attribute 'declare_publisher'` → 节点崩溃、
        #   监督器重启 → 加入方一连上就反复挂（实测在那台 NAS 上就是这么挂的）。
        if self._remote is None:
            try:
                self._remote = zenoh.open(self._remote_config())
            except Exception as exc:  # noqa: BLE001
                # ⚠️ 跨机 session 起不来（证书丢了 / 端口被占 / 樱花没通）**不能让节点死**：
                #    死一颗就进监督器重启循环刷屏，而且本机房间的消息也再没人转发。
                #    降级为「只本机桥接」——本机 12 个节点照常互通（自动发现的对端仍可用）。
                self.log.error("跨机 session 打开失败 → 降级为「只本机桥接」：%s", exc)
                self._remote = None
        if self._remote is not None:
            self._remote_sub = self._remote.declare_subscriber(
                self._link_subscribe_key(), self._on_remote_sample
            )
            self._hello_pub = self._remote.declare_publisher(self._hello_key(self.role))
        else:
            self.log.warning("bridge 以「只本机」模式运行：跨机链路不可用（原因见上面的 error 行）")

        self._setup_local_subscriptions()

        if self._remote is not None:
            self._send_hello()
            if self.hello_interval > 0:
                self._hello_task = asyncio.create_task(self._hello_loop())

        if not self._listen and not self._connect:
            self.log.warning(
                "bridge 未配置任何跨机 listen/connect endpoint —— 只能桥接已自动发现的对端"
            )
        self.log.info(
            "bridge 就绪：role=%s peer=%s link_room=%s 白名单=%d 条 encrypt=%s",
            self.role,
            self.peer_role,
            self.link_room,
            len(topics.CROSS_MACHINE_ALLOWLIST),
            self.encrypt,
        )

    async def teardown(self) -> None:
        task, self._hello_task = self._hello_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        for sub in self._local_subs:
            self._undeclare(sub)
        self._local_subs.clear()
        self._undeclare(self._remote_sub)
        self._remote_sub = None
        self._undeclare(self._hello_pub)
        self._hello_pub = None
        for pub in self._remote_pubs.values():
            self._undeclare(pub)
        self._remote_pubs.clear()
        for pub in self._local_pubs.values():
            self._undeclare(pub)
        self._local_pubs.clear()

        remote, self._remote = self._remote, None
        if remote is not None and self._owned_remote:
            try:
                remote.close()
            except Exception as exc:  # noqa: BLE001
                self.log.error("跨机 session.close() 失败", exc_info=exc)
            else:
                self.log.debug("跨机 session 已关闭")

    # ──────────────────────────────────────────────────────────────
    # 加密：密钥就是房间码
    # ──────────────────────────────────────────────────────────────
    def _refresh_key(self) -> None:
        """按当前 `link_room` 重算 AEAD 开关与密钥。

        **一个房间码搞定房间 + 链路**：链路房间就是房间码，密钥由它经 HKDF 派生
        （`crypto.derive_key`）—— 两端在面板上选同一个房间码，加密链路就配好了，
        不用再单独记一个 `[bridge] room_code`。

        规则：

        - 显式配了 `encrypt = true/false` → **照做**（true 时密钥仍由房间码派生）；
        - 没配 → 有"真房间"（不是默认房间 `local`）就加密，否则不加密；
        - 无论哪条路，房间**弱**（默认房间 / 不像随机房间码）都**大声警告**：
          那种密钥是猜得出来的，加密只是"看着像加密"。诚实比体面重要。
        """
        self.encrypt = False
        self._key = None

        if self._encrypt_requested is False:
            return

        weak_reason = ""
        if self.link_room == topics.DEFAULT_ROOM:
            weak_reason = (
                f"链路房间是默认的 {self.link_room!r}，它的密钥人人可推（等于没加密）"
            )
        elif not _looks_like_room_code(self.link_room):
            weak_reason = (
                f"链路房间 {self.link_room!r} 不像随机房间码，密钥容易被猜"
            )

        if self._encrypt_requested is None and weak_reason:
            # 没显式要求 + 房间弱 → 干脆不加密（"看着像加密"比明文更危险），只警告
            self.log.warning(
                "跨机链路未加密：%s。想要加密链路就在面板上创建/加入一个房间码"
                "（或显式配 [bridge] room_code），密钥会由它派生。"
                "执意要加密请显式写 encrypt = true。",
                weak_reason,
            )
            return

        self.encrypt = True
        self._key = crypto.derive_key(self.link_room)
        if weak_reason:
            self.log.warning(
                "跨机链路按你的要求加密了，但 %s —— 只能算混淆，挡不住有心人。", weak_reason
            )

    # ──────────────────────────────────────────────────────────────
    # 组装（拆成小方法，便于单测直接调用）
    # ──────────────────────────────────────────────────────────────
    def _setup_local_subscriptions(self) -> None:
        """本机侧：**逐条**订阅白名单 key。

        注意这里是"逐条"而不是订一个 ``**`` 再过滤 —— ``feature/frame.raw``
        **不在白名单里**，所以它连一条订阅都没有，原始帧根本不会进入本进程。
        """
        for suffix in topics.CROSS_MACHINE_ALLOWLIST:
            key = topics.key(suffix, self.room)
            handler = self._make_local_handler(suffix)
            self._local_subs.append(self.session.declare_subscriber(key, handler))
        self.log.debug("本机订阅 %d 条白名单 key", len(self._local_subs))

    async def on_room_switched(self, old_room: str, new_room: str) -> None:
        """换房间后把**本机侧**的白名单订阅/发布重来一遍。

        为什么 bridge 要特殊照顾：它这套 pub/sub 是直接拿**原生 session** 声明的
        （`self.session.declare_subscriber(...)`，见 :meth:`_setup_local_subscriptions`），
        不在基类那张 `_registrations` 表里 —— 基类帮不上忙。

        链路侧要分两种情况（`_link_room_follows_room`）：

        - 显式配了 `[bridge] room_code` → 链路房间**钉死**，跨机那套 key 不动
          （它是"两条业务房间之间的通道"，跟业务房间是两码事）；
        - 没配（默认）→ 链路房间**就是业务房间**，所以链路侧也得跟着重建，
          否则本机换到新房间、链路还订在旧前缀上 —— 跨机转发整条断掉且不报错。
        """
        for sub in self._local_subs:
            self._undeclare(sub)
        self._local_subs.clear()
        for pub in self._local_pubs.values():
            self._undeclare(pub)
        self._local_pubs.clear()
        self._setup_local_subscriptions()

        if self._link_room_follows_room:
            self._rebuild_link_side(new_room)
            self.log.info(
                "房间 %s → %s：本机白名单 + 跨机链路都重声明了（链路房间也跟着走）",
                old_room, new_room,
            )
        else:
            self.log.info(
                "房间 %s → %s：本机白名单已重声明；跨机链路房间固定为 %s，不动",
                old_room, new_room, self.link_room,
            )

    def _rebuild_link_side(self, new_room: str) -> None:
        """链路房间跟着业务房间走时，把跨机侧的订阅/发布/密钥也换掉。"""
        self._undeclare(self._remote_sub)
        self._undeclare(self._hello_pub)
        self.link_room = new_room
        self._refresh_key()          # 密钥跟着房间码走（一个房间码搞定房间 + 链路）
        # 去重表里的 key 都带旧链路 → 换房间后直接丢掉（不清会让新链路的第一条被误吞）
        self._injected.clear()
        self._injected_order.clear()
        self._remote_pubs.clear()
        self.peer_info = None
        self._peer_connected = False
        if self._remote is not None:
            self._remote_sub = self._remote.declare_subscriber(
                self._link_subscribe_key(), self._on_remote_sample
            )
            self._hello_pub = self._remote.declare_publisher(self._hello_key(self.role))
            self._send_hello()

    def _remote_config(self) -> zenoh.Config:
        cfg = zenoh.Config()
        if self._listen:
            cfg.insert_json5("listen/endpoints", json.dumps(list(self._listen)))
        if self._connect:
            cfg.insert_json5("connect/endpoints", json.dumps(list(self._connect)))
        for key, value in self._remote_json5.items():
            cfg.insert_json5(str(key), value if isinstance(value, str) else json.dumps(value))
        return cfg

    # ──────────────────────────────────────────────────────────────
    # key 计算
    # ──────────────────────────────────────────────────────────────
    def _link_key(self, from_role: str, suffix: str) -> str:
        return topics.key(f"{LINK_LAYER}/{from_role}/{suffix}", self.link_room)

    def _hello_key(self, role_of_sender: str) -> str:
        return self._link_key(role_of_sender, HELLO_SUFFIX)

    def _link_subscribe_key(self) -> str:
        """订阅**对端**发射的一切：``coyotelink/<link_room>/bridge/<peer>/**``。"""
        return topics.key(f"{LINK_LAYER}/{self.peer_role}/{topics.WILDCARD_ALL}", self.link_room)

    def parse_link_key(self, key: str) -> tuple[str, str]:
        """链路 key → ``(from_role, suffix)``；不是 bridge 链路 key 则 ``ValueError``。"""
        if topics.room_of(key) != self.link_room:
            raise ValueError(f"链路 key 的 room 不是 {self.link_room!r}：{key!r}")
        suffix_full = topics.suffix_of(key)  # "bridge/<role>/<suffix...>"
        parts = suffix_full.split("/", 2)
        if len(parts) < 3 or parts[0] != LINK_LAYER:
            raise ValueError(f"不是 bridge 链路 key：{key!r}")
        return parts[1], parts[2]

    # ──────────────────────────────────────────────────────────────
    # 本机 → 跨机
    # ──────────────────────────────────────────────────────────────
    def _make_local_handler(self, suffix: str) -> Callable[[Any], None]:
        def handler(sample: Any) -> None:
            try:
                data = bytes(sample.payload)
            except Exception:  # noqa: BLE001 - 取不到就跳过，别打死节点
                return
            if self._consume_injected(suffix, data):
                self.stats["loop_skipped"] += 1
                return  # 自己刚注入的，别回环
            self.forward_to_remote(suffix, data)

        return handler

    def forward_to_remote(self, suffix: str, data: bytes) -> None:
        """把本机一条白名单消息转发到跨机链路。"""
        if self._remote is None:
            # 远端 session 还没就绪（启动那一瞬，或远端打开失败）→ **丢这一条**，
            # 别拉崩整条链路。跨机链路允许丢早期消息（hello 会周期重播补上）。
            self.stats["dropped"] += 1
            return
        out = self._encode(data, suffix)
        try:
            self._remote_pub(suffix).put(out)
        except Exception as exc:  # noqa: BLE001 - 发不出去必须外显
            self._fatal(exc, where=f"跨机转发 {suffix}")
            return
        self.stats["out"] += 1
        # ⚠️ 必须留痕：这条链路上三个环节（本机订阅 / 链路 / 对端注入）以前都**不打日志**，
        #    一旦跨机不通，日志里干干净净，只能靠猜（实测为此烧掉过半小时）。
        self.log.debug("本机 → 跨机 %s（%d 字节%s）", suffix, len(out),
                       "，已加密" if self.encrypt else "")

    def _remote_pub(self, suffix: str) -> Any:
        pub = self._remote_pubs.get(suffix)
        if pub is None:
            pub = self._remote.declare_publisher(self._link_key(self.role, suffix))
            self._remote_pubs[suffix] = pub
        return pub

    # ──────────────────────────────────────────────────────────────
    # 跨机 → 本机
    # ──────────────────────────────────────────────────────────────
    def _on_remote_sample(self, sample: Any) -> None:
        try:
            key = str(sample.key_expr)
        except Exception:  # noqa: BLE001
            key = "<unknown>"
        try:
            from_role, suffix = self.parse_link_key(key)
        except ValueError as exc:
            self.log.warning("跨机收到无法解析的 key，丢弃：%s（%s）", key, exc)
            self.stats["dropped"] += 1
            return

        if from_role == self.role:
            # 自己的发射（理论上不会订阅到），忽略即可
            return
        if from_role != self.peer_role:
            self.log.debug("忽略非对端角色的链路消息（role=%s）：%s", from_role, key)
            return

        if suffix == HELLO_SUFFIX:
            self._on_peer_hello(bytes(sample.payload))
            return

        # 🔒 第二道闸门：白名单硬约束（对端乱塞也进不来）
        if not topics.is_cross_machine(suffix):
            self.log.warning("跨机链路收到非白名单 key，拒绝放行：%s", key)
            self.stats["dropped"] += 1
            return

        data = bytes(sample.payload)
        try:
            data = self._decode(data, suffix)
        except crypto.SealError as exc:
            self.log.warning("跨机 payload 解密失败，丢弃 %s：%s", key, exc)
            self.stats["dropped"] += 1
            return
        self.log.debug("跨机 → 本机 %s（%d 字节）", suffix, len(data))
        self.inject_local(suffix, data)

    def inject_local(self, suffix: str, data: bytes) -> None:
        """把对端一条消息发布到本机（下游节点据此收到）。"""
        self._mark_injected(suffix, data)
        try:
            self._local_pub(suffix).put(data)
        except Exception as exc:  # noqa: BLE001
            self._fatal(exc, where=f"注入本机 {suffix}")
            return
        self.stats["in"] += 1

    def _local_pub(self, suffix: str) -> Any:
        pub = self._local_pubs.get(suffix)
        if pub is None:
            pub = self.session.declare_publisher(topics.key(suffix, self.room))
            self._local_pubs[suffix] = pub
        return pub

    # ──────────────────────────────────────────────────────────────
    # 注入去重（破本机自环）
    # ──────────────────────────────────────────────────────────────
    def _mark_injected(self, suffix: str, data: bytes) -> None:
        token = (suffix, data)
        self._injected[token] = self._injected.get(token, 0) + 1
        self._injected_order.append(token)
        while len(self._injected_order) > _INJECTED_CAP:
            old = self._injected_order.popleft()
            remaining = self._injected.get(old, 0) - 1
            if remaining <= 0:
                self._injected.pop(old, None)
            else:
                self._injected[old] = remaining

    def _consume_injected(self, suffix: str, data: bytes) -> bool:
        token = (suffix, data)
        remaining = self._injected.get(token, 0)
        if remaining <= 0:
            return False
        if remaining == 1:
            self._injected.pop(token, None)
        else:
            self._injected[token] = remaining - 1
        return True

    # ──────────────────────────────────────────────────────────────
    # 可选 E2E 加密
    # ──────────────────────────────────────────────────────────────
    def _encode(self, data: bytes, suffix: str) -> bytes:
        if self._key is None:
            return data
        return crypto.seal(self._key, data, aad=suffix.encode("utf-8"))

    def _decode(self, data: bytes, suffix: str) -> bytes:
        if self._key is None:
            # ⚠️ 本端未启用 E2E 加密时，若对端发来的却是本模块的密文（带 MAGIC），
            #    说明**两端配置不一致**（对端 encrypt=true）——必须拒绝，不能当明文收下，
            #    否则密文会被当成垃圾数据注入下游。实测踩过：不判 MAGIC 就会静默注入。
            if data.startswith(crypto.MAGIC):
                raise crypto.SealError(
                    "本端未启用 E2E 加密，但收到密文（对端 encrypt=true）—— 配置不一致，已丢弃"
                )
            return data
        return crypto.open_sealed(self._key, data, aad=suffix.encode("utf-8"))

    # ──────────────────────────────────────────────────────────────
    # 握手 / 角色协商 / 对端状态
    # ──────────────────────────────────────────────────────────────
    def hello_payload(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "name": self.name,
            "room": self.link_room,
            "ts": int(time.time() * 1000),
            "v": 1,
            "caps": list(topics.CROSS_MACHINE_ALLOWLIST),
        }

    def _send_hello(self) -> None:
        if self._hello_pub is None:
            return
        payload = json.dumps(self.hello_payload(), ensure_ascii=False).encode("utf-8")
        try:
            self._hello_pub.put(payload)
        except Exception as exc:  # noqa: BLE001 - hello 失败不致命，但要可见
            self.log.warning("hello 发送失败：%s", exc)

    def _on_peer_hello(self, data: bytes) -> None:
        try:
            info = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.log.debug("忽略无法解析的 hello：%s", exc)
            return
        if not isinstance(info, dict):
            return
        info["_at"] = time.time()
        previous_role = (self.peer_info or {}).get("role")
        self.peer_info = info
        self._last_hello = time.time()
        role_now = info.get("role")
        # 首次连上、或对端角色变了 → 记 INFO + 回调；其余 hello 只记 DEBUG（别刷屏）
        if not self._peer_connected or previous_role != role_now:
            self._peer_connected = True
            self.log.info(
                "对端 online：role=%s name=%s room=%s", role_now, info.get("name"), info.get("room")
            )
            self._emit_peer_state("connected")
        else:
            self.log.debug("对端 hello：role=%s", role_now)

    async def _hello_loop(self) -> None:
        while True:
            await asyncio.sleep(self.hello_interval)
            self._send_hello()
            if self._peer_connected and self._last_hello:
                if time.time() - self._last_hello > self.hello_interval * 3:
                    self._peer_connected = False
                    self.log.info("对端 hello 超时，判定 offline")
                    self._emit_peer_state("disconnected")

    def _emit_peer_state(self, state: str) -> None:
        callback = self.on_peer_state
        if callback is None:
            return
        try:
            callback(state)
        except Exception as exc:  # noqa: BLE001 - 回调出错不能打死节点
            self.log.warning("on_peer_state 回调出错：%s", exc)

    # ──────────────────────────────────────────────────────────────
    # 工具
    # ──────────────────────────────────────────────────────────────
    @staticmethod
    def _undeclare(handle: Any) -> None:
        if handle is None:
            return
        try:
            handle.undeclare()
        except Exception:  # noqa: BLE001 - 撤销失败不重要（session 关闭会兜底）
            pass

    # ──────────────────────────────────────────────────────────────
    # CLI 入口（比基类多 bridge 专属参数）
    # ──────────────────────────────────────────────────────────────
    @classmethod
    def main(cls, argv: list[str] | None = None) -> int:  # type: ignore[override]
        force_utf8()
        parser = argparse.ArgumentParser(
            prog="python -m agent.nodes.bridge",
            description="跨机白名单桥接节点（T3）",
        )
        parser.add_argument("--config", default=None, help="agent/config.toml 路径")
        parser.add_argument("--room", default=None, help="覆盖**本机** room")
        parser.add_argument("--log-level", default=None)
        parser.add_argument("--log-dir", default=None)
        parser.add_argument("--role", default=None, choices=list(room.ROLES),
                            help="本端角色 a/b（默认跟 `[bridge] role`，都没有才是 a）")
        parser.add_argument("--room-code", "--link-room", dest="room_code",
                            default=None, help="房间码（链路 room 层）")
        parser.add_argument("--listen", action="append", default=None,
                            help="跨机监听 endpoint（可重复），如 tcp/0.0.0.0:7451")
        parser.add_argument("--connect", action="append", default=None,
                            help="跨机连接 endpoint（可重复），如 tcp/<对端>:7451")
        parser.add_argument("--encrypt", action="store_true", default=None,
                            help="启用应用层 AEAD（默认关）")
        args = parser.parse_args(argv)

        config = Config.load(args.config)
        if args.room:
            config.room = args.room
        if args.log_level:
            config.log_level = normalize_level(args.log_level)
        if args.log_dir:
            config.log_dir = Path(args.log_dir).resolve()

        node = cls(
            config,
            role=args.role,
            link_room=args.room_code,
            listen=args.listen,
            connect=args.connect,
            encrypt=args.encrypt,
        )
        return run_node(node)


#: supervisor / ``cli run`` 用的默认工厂
NODE = BridgeNode


if __name__ == "__main__":  # pragma: no cover - 便于 `python -m agent.nodes.bridge`
    raise SystemExit(BridgeNode.main())
