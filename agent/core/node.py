"""Node 基类 —— 让"写一个节点"只需三步：继承 → 声明 → 写业务。

接口契约来源：`docs/tasks/TF-节点框架与消息总线.md` §4.2 / §4.2.1。

目标形态：

    from agent.core import Node, topics

    class PoseNode(Node):
        name = "pose"

        async def setup(self) -> None:
            self.sub = self.subscribe(topics.FEATURE_FRAME_RAW, self.on_frame)
            self.pub = self.publish(topics.FEATURE_POSE)

        async def on_frame(self, msg: dict) -> None:
            ...

    if __name__ == "__main__":
        raise SystemExit(PoseNode.main())

三个实测坑（都已在本文件里消化掉，节点作者不用管）：

1. **Zenoh 回调跑在非主线程**（`Thread-1 (pyo3-closure)`）→ `async def` handler 必须
   用 `asyncio.run_coroutine_threadsafe` 桥接回事件循环，否则**静默不执行**；
2. **Windows 上 `terminate()` 捕获不到** → 优雅退出必须注册
   `SIGINT` + `SIGTERM` + `SIGBREAK` 三件套；
3. **`Query.reply()` 必须传两个参数** → `Query` 薄包装自动补 key，节点作者踩不到。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import inspect
import os
import signal
import socket
import sys
import threading
import time
import traceback
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, ClassVar, Sequence

import zenoh

from . import messages, roomstate, topics
from .config import Config, force_utf8, normalize_level
from .logging import close_handlers, setup_logging

#: 事件循环"卡住"超过这么多秒 → 看门狗把线程栈打进该节点的日志（见 `Node._start_stall_watchdog`）
_STALL_AFTER_S = 20.0

#: 本机主机名（心跳里带上，面板用来"认机器"：开火打谁 / 谁在控制我）
_HOSTNAME = socket.gethostname()

__all__ = [
    "NodeError",
    "Node",
    "Publisher",
    "Subscriber",
    "Queryable",
    "Query",
    "run_node",
]

_MISSING = object()


class NodeError(RuntimeError):
    """节点框架层面的错误（用法错、生命周期错、无法恢复的运行时错）。"""


def _is_async_callable(obj: Any) -> bool:
    """`obj` 是否是异步可调用（函数 / 方法 / 实现了 `async __call__` 的对象）。"""
    if inspect.iscoroutinefunction(obj):
        return True
    call = getattr(obj, "__call__", None)
    return call is not None and inspect.iscoroutinefunction(call)


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except (ValueError, AttributeError):  # pragma: no cover
        return f"signal {signum}"


# ──────────────────────────────────────────────────────────────
# 发布 / 订阅 / 查询 的薄包装
# ──────────────────────────────────────────────────────────────
class Publisher:
    """`Node.publish()` 的返回值：自动 JSON 序列化（`ensure_ascii=False`）。"""

    def __init__(self, node: "Node", topic: str) -> None:
        self._node = node
        self._topic = topic            # 声明时给的（后缀，或显式完整 key）
        self.key = topics.resolve(topic, node.room)   # 当前 room 下的完整 key
        self._raw: Any = None

    @property
    def raw(self) -> Any:
        """原生 `zenoh.Publisher`（逃生舱）。"""
        return self._raw

    def declare(self, session: Any) -> None:
        """（重新）声明发布者。

        ★ 换房间时**这个 wrapper 对象不换**，只换里面的原生 publisher ——
        节点代码普遍把 Publisher 存进了自己的属性（`self._pub_fire` 这种），
        换对象等于把它们全废掉（见 `Node.switch_room`）。
        后缀跟着新 room 走；声明时就给的**显式完整 key** 保持原样（那是明确指定的）。
        """
        self.key = topics.resolve(self._topic, self._node.room)
        self._raw = session.declare_publisher(self.key)

    def __repr__(self) -> str:
        return f"<Publisher {self.key}>"

    def put(self, payload: Any = None) -> None:
        """发一条消息。序列化失败与发送失败都会**外显**（记日志 + 节点退出码非 0）。"""
        if self._node._switching:
            # 换房间的窗口期：session 正在重开，这时发出去必然失败。
            # ⚠️ 这一条不能走 _fatal —— device 节点的注册表轮询线程、
            #    sim_coyote 的 2s 轮询都可能正好在这个窗口里发消息，
            #    而"发不出去"是预期内的，不该把节点打死。
            self._node.log.debug("换房间进行中，丢弃一条 %s", self.key)
            return
        try:
            data = messages.to_bytes(payload)
        except messages.MessageError as exc:
            self._node._fatal(exc, where=f"序列化 {self.key}")
            return
        try:
            self._raw.put(data)
        except Exception as exc:  # noqa: BLE001 - 必须外显，不能吞
            self._node._fatal(exc, where=f"发布 {self.key}")

    # 任务书 §4.2.1 写作 `.put(...)`，§5.6 的例子写作 `.publish(...)` —— 两个都支持
    publish = put

    def undeclare(self) -> None:
        if self._raw is None:
            return
        try:
            self._raw.undeclare()
        except Exception as exc:  # noqa: BLE001
            self._node.log.debug("撤销发布者 %s 失败：%s", self.key, exc)
        self._raw = None


class Subscriber:
    """`Node.subscribe()` 的返回值。

    与 :class:`Publisher` 同理：换房间时 wrapper 身份保持，只重新声明内部对象 ——
    而且分发用的 key 是**回调那一刻**从 `self.key` 读的，不会带着旧房间的 key 去记账。
    """

    def __init__(
        self, node: "Node", topic: str, handler: Callable[..., Any], *, with_key: bool = False
    ) -> None:
        self._node = node
        self._topic = topic
        self._handler = handler
        self._with_key = with_key
        self.key = topics.resolve(topic, node.room)
        self._raw: Any = None

    @property
    def raw(self) -> Any:
        return self._raw

    def _on_sample(self, sample: Any) -> None:
        self._node._handle_sample(self.key, sample, self._handler, self._with_key)

    def declare(self, session: Any) -> None:
        """（重新）声明订阅（见 :meth:`Publisher.declare`）。"""
        self.key = topics.resolve(self._topic, self._node.room)
        self._raw = session.declare_subscriber(self.key, self._on_sample)

    def __repr__(self) -> str:
        return f"<Subscriber {self.key}>"

    def undeclare(self) -> None:
        if self._raw is None:
            return
        try:
            self._raw.undeclare()
        except Exception as exc:  # noqa: BLE001
            self._node.log.debug("撤销订阅 %s 失败：%s", self.key, exc)
        self._raw = None


class Query:
    """`declare_queryable()` 的 handler 收到的对象（薄包装，防错优先）。

    - `q.key` / `q.data` / `q.json()` 读请求
    - `q.reply(payload)` **自动补 key** —— 避开原生 `Query.reply()` 必须传两个参数的坑
    - `q.raw` 拿原生 `zenoh.Query`（逃生舱）
    """

    def __init__(self, node: "Node", raw: Any, reply_key: str) -> None:
        self._node = node
        self._raw = raw
        self.key = str(raw.key_expr)
        self._reply_key = reply_key
        self.replied = False

    @property
    def raw(self) -> Any:
        return self._raw

    @property
    def data(self) -> bytes:
        """请求体（无 body 时是 `b""`）。"""
        payload = getattr(self._raw, "payload", None)
        return bytes(payload) if payload is not None else b""

    #: 别名，与订阅侧 `payload` 的叫法保持一致
    payload = data

    @property
    def parameters(self) -> str:
        return getattr(self._raw, "parameters", "") or ""

    def __repr__(self) -> str:
        return f"<Query {self.key}>"

    def json(self, default: Any = _MISSING) -> Any:
        """请求体当 JSON 解；空 body 返回 None，解不开时按 `default` 决定是否抛。"""
        data = self.data
        if not data:
            return None
        try:
            return messages.from_bytes(data)
        except messages.MessageError:
            if default is _MISSING:
                raise
            return default

    def reply(self, payload: Any = None, *, key: str | None = None) -> None:
        self.replied = True
        self._raw.reply(key or self._reply_key, messages.to_bytes(payload))

    def reply_error(self, payload: Any) -> None:
        self.replied = True
        self._raw.reply_err(messages.to_bytes(payload))


class Queryable:
    """`Node.declare_queryable()` 的返回值。"""

    def __init__(self, node: "Node", topic: str, handler: Callable[..., Any]) -> None:
        self._node = node
        self._topic = topic
        self._handler = handler
        self.key = topics.resolve(topic, node.room)
        self._raw: Any = None

    @property
    def raw(self) -> Any:
        return self._raw

    def _on_query(self, raw_query: Any) -> None:
        # key 在回调那一刻取：换过房间之后不能拿旧房间的 key 去记账
        self._node._handle_query(self.key, raw_query, self._handler)

    def declare(self, session: Any) -> None:
        """（重新）声明查询服务（见 :meth:`Publisher.declare`）。"""
        self.key = topics.resolve(self._topic, self._node.room)
        self._raw = session.declare_queryable(self.key, self._on_query)

    def __repr__(self) -> str:
        return f"<Queryable {self.key}>"

    def undeclare(self) -> None:
        if self._raw is None:
            return
        try:
            self._raw.undeclare()
        except Exception as exc:  # noqa: BLE001
            self._node.log.debug("撤销查询服务 %s 失败：%s", self.key, exc)
        self._raw = None


# ──────────────────────────────────────────────────────────────
# Node
# ──────────────────────────────────────────────────────────────
class Node(ABC):
    """节点基类。一个进程 = 一个 Node 实例（不要在模块级建 session）。"""

    #: 子类**必须**定义，例如 `name = "pose"`
    name: ClassVar[str] = ""
    #: 覆盖配置里的 room；None 表示用 `Config.room`
    room: ClassVar[str | None] = None
    #: 心跳间隔秒；None 用 `Config.heartbeat_interval_s`，0 = 关闭
    heartbeat_interval: ClassVar[float | None] = None
    #: `teardown()` 超时秒；None 用 `Config.teardown_timeout_s`
    teardown_timeout: ClassVar[float | None] = None
    #: `_wait_for_stop()` 的轮询间隔（见该方法的注释）
    stop_poll_interval: ClassVar[float] = 0.2
    #: 一次性 CLI 命令用：不落盘日志 + 日志走 stderr（stdout 留给管道数据）
    cli_mode: ClassVar[bool] = False

    def __init__(self, config: Config | None = None) -> None:
        cls = type(self)
        if not getattr(cls, "name", ""):
            raise NodeError(f'{cls.__name__} 必须定义类属性 name（例如 name = "pose"）')

        self.config = config if config is not None else Config.load()
        self.room: str = getattr(cls, "room", None) or self.config.room
        cli_mode = bool(getattr(cls, "cli_mode", False))
        self.log = setup_logging(
            self.name,
            level=self.config.log_level,
            log_dir=None if cli_mode else self.config.log_dir,
            log_file=not cli_mode,
            stream=sys.stderr if cli_mode else None,
        )

        self._session: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._stop_requested = False
        self._closing = False
        self._shutdown_done = False
        self._teardown_called = False
        self._state_lock = threading.Lock()

        self._publishers: dict[str, Publisher] = {}
        self._subscribers: list[Subscriber] = []
        self._queryables: list[Queryable] = []
        #: ★ 声明过的 pub/sub/queryable（wrapper 身份不变）—— 换房间时按它重新声明
        self._registrations: list[Any] = []
        #: ★ 换房间的窗口期标记（见 `Publisher.put` 的护栏）
        self._switching = False
        self._status_publisher: Publisher | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self._heartbeat_seq = 0

        self._error: BaseException | None = None
        self._started = threading.Event()
        self._started_at = time.time()
        self._signals: tuple[str, ...] = ()
        self._previous_signals: dict[int, Any] = {}

    # —— 基本信息 ——
    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} room={self.room!r}>"

    @property
    def error(self) -> BaseException | None:
        """节点内第一个未捕获异常（None = 一切正常）。"""
        return self._error

    @property
    def running(self) -> bool:
        return self._session is not None and not self._closing

    @property
    def session(self) -> Any:
        """原生 `zenoh.Session`（逃生舱）。未启动时抛错，避免"静默拿到 None"。"""
        if self._session is None:
            raise NodeError(
                f"节点 {self.name} 的 Zenoh session 尚未打开"
                "（只能在 setup() 之后、退出之前使用）"
            )
        return self._session

    def wait_started(self, timeout: float | None = None) -> bool:
        """阻塞等待 `setup()` 结束（跨线程安全，测试/工具用）。"""
        return self._started.wait(timeout)

    # ──────────────────────────────────────────────────────────
    # 子类实现
    # ──────────────────────────────────────────────────────────
    @abstractmethod
    async def setup(self) -> None:
        """声明订阅/发布、初始化资源。"""

    async def teardown(self) -> None:
        """清理资源。默认空实现。"""
        return None

    # ──────────────────────────────────────────────────────────
    # 便捷方法（都在 setup() 里调）
    # ──────────────────────────────────────────────────────────
    def resolve_key(self, topic: str, *, where: str = "") -> str:
        """把后缀补全成完整 key，并对"room 不一致"给出可操作的告警。

        为什么要这一步：`topics.feature()` / `topics.test()` 这些函数**默认 room='local'**，
        调用时若忘了传 room，返回的就是 `<前缀>/local/...` 这种完整 key；而 `resolve()`
        见到完整 key 会原样放行 —— 于是非 local room 的节点会**静默订到别的房间**，
        现象是"收不到数据但不报错"（TF 开发时实测踩过）。这里把它变成一条明确 warning。

        后缀常量（`topics.FEATURE_POSE` 这类）不带 room，写节点时优先用它。
        """
        key = topics.resolve(topic, self.room)
        if topics.is_full_key(key) and topics.room_of(key) != self.room:
            self.log.warning(
                "key %s 的 room 是 %r，与本节点 room %r 不一致%s\n"
                "  若想用本节点的 room：传后缀（如 topics.FEATURE_POSE），"
                "或 topics.feature(..., self.room)",
                key,
                topics.room_of(key),
                self.room,
                f"（{where}）" if where else "",
            )
        return key

    def publish(self, topic: str, *, reuse: bool = True) -> Publisher:
        """声明发布者。同一个 key 重复调用默认复用同一个 Publisher。"""
        key = self.resolve_key(topic, where="publish")
        if reuse:
            existing = self._publishers.get(key)
            if existing is not None:
                return existing
        publisher = Publisher(self, topic)
        publisher.declare(self.session)
        self._publishers[publisher.key] = publisher
        self._registrations.append(publisher)
        self.log.debug("声明发布 → %s", publisher.key)
        return publisher

    def subscribe(
        self, topic: str, handler: Callable[..., Any], *, with_key: bool = False
    ) -> Subscriber:
        """声明订阅。

        - `with_key=False`（默认）→ `handler(msg)`
        - `with_key=True`          → `handler(key, msg)`

        handler 可同步可 `async`（基类负责桥接）。**坏消息不会打死节点**：
        JSON 解不开时原样传 `bytes` 并记 warning，继续运行。
        """
        if not callable(handler):
            raise TypeError(f"subscribe() 的 handler 必须可调用，得到 {type(handler).__name__}")
        self.resolve_key(topic, where="subscribe")     # 只为那条"room 不一致"的告警

        subscriber = Subscriber(self, topic, handler, with_key=with_key)
        subscriber.declare(self.session)
        self._subscribers.append(subscriber)
        self._registrations.append(subscriber)
        self.log.debug("声明订阅 ← %s（with_key=%s）", subscriber.key, with_key)
        return subscriber

    def declare_queryable(self, topic: str, handler: Callable[..., Any]) -> Queryable:
        """声明查询服务（请求-响应）。

        `handler(q)` 可同步可 `async`；`q.reply(payload)` 自动补 key；
        handler 返回非 None 且尚未 reply 时**自动 reply**。
        """
        if not callable(handler):
            raise TypeError(f"declare_queryable() 的 handler 必须可调用，得到 {type(handler).__name__}")
        self.resolve_key(topic, where="declare_queryable")

        queryable = Queryable(self, topic, handler)
        queryable.declare(self.session)
        self._queryables.append(queryable)
        self._registrations.append(queryable)
        self.log.debug("声明查询服务 ⇄ %s", queryable.key)
        return queryable

    def query(self, topic: str, payload: Any = None, timeout: float = 2.0) -> bytes | None:
        """发一次查询并等回复，返回回复 body；超时/无回复返回 None。

        **同步阻塞**（可在任意线程调用）。`timeout` 必填 —— 避免节点间互等死锁（任务书 §6）。
        """
        key = self.resolve_key(topic, where="query")
        body = None if payload is None else messages.to_bytes(payload)
        session = self.session
        try:
            replies = (
                session.get(key, timeout=timeout)
                if body is None
                else session.get(key, payload=body, timeout=timeout)
            )
            for reply in replies:
                ok = getattr(reply, "ok", None)
                if ok is not None:
                    return bytes(ok.payload)
                err = getattr(reply, "err", None)
                if err is not None:
                    detail = getattr(err, "payload", None)
                    self.log.warning(
                        "query %s 返回错误：%r",
                        key,
                        bytes(detail) if detail is not None else err,
                    )
        except Exception as exc:  # noqa: BLE001 - 查询失败不能打死节点
            self.log.warning("query %s 失败：%s", key, exc)
            return None
        return None

    def query_json(self, topic: str, payload: Any = None, timeout: float = 2.0) -> Any:
        """`query()` 的 JSON 版；解不开或无回复返回 None。"""
        raw = self.query(topic, payload, timeout)
        if not raw:
            return None
        try:
            return messages.from_bytes(raw)
        except messages.MessageError as exc:
            self.log.warning("query %s 的回复无法解码：%s", topic, exc)
            return None

    # ──────────────────────────────────────────────────────────
    # 健康
    # ──────────────────────────────────────────────────────────
    def status_payload(self) -> dict[str, Any]:
        now = time.time()
        return {
            "ts": int(now * 1000),
            "name": self.name,
            # ★ 主机名：同一个房间里可能有多台机器，面板要能"认人"（开火指定打谁、
            #   被控端视图显示"谁在控制我"）。以前只有 name/pid，跨机时认不出是哪台。
            "host": _HOSTNAME,
            "room": self.room,
            "pid": os.getpid(),
            "uptime_s": round(now - self._started_at, 3),
            "seq": self._heartbeat_seq,
        }

    def heartbeat(self) -> None:
        """向 `status/<name>` 发一次心跳（基类默认按 `heartbeat_interval` 定时发）。"""
        session = self._session
        if session is None:
            return
        publisher = self._status_publisher
        if publisher is None:
            # 用**后缀**建，这样换房间后 key 会跟着新 room（换房时也会把它清掉重建）
            publisher = Publisher(self, f"{topics.STATUS_PREFIX}{self.name}")
            publisher.declare(session)
            self._status_publisher = publisher
        self._heartbeat_seq += 1
        publisher.put(self.status_payload())

    # ──────────────────────────────────────────────────────────
    # 房间（key 前缀）
    # ──────────────────────────────────────────────────────────
    def _subscribe_room_command(self) -> None:
        """订阅 `command/room` —— 面板换房间时，各节点跟着换 key 前缀。

        刻意放在基类：**每个**节点都得跟着换，不然它就留在旧房间里自言自语。
        """
        self.subscribe(topics.COMMAND_ROOM, self.on_room_command)

    async def on_room_command(self, msg: Any) -> None:
        """`command/room` 的默认处理：create / join / leave → 换房间。

        payload（与 `web/js/room.js` 商定）：``{"action": …, "code": "AB12CD", "url": …}``。
        `leave` = 回默认房间（`local`）。
        """
        if not isinstance(msg, dict):
            return
        action = str(msg.get("action") or "").strip().lower()
        code = str(msg.get("code") or "").strip()
        if action == "leave":
            code = topics.DEFAULT_ROOM
        if not code:
            return
        if code == self.room:
            self.log.debug("已经在房间 %s，忽略本次换房", code)
            return
        await self.switch_room(code)

    async def on_room_switched(self, old_room: str, new_room: str) -> None:
        """换房间之后的钩子 —— 给**绕过基类注册表**的节点用。

        走 `self.publish/subscribe/declare_queryable` 的节点什么都不用做
        （基类已经按新 room 重新声明好了）；直接拿原生 session 声明 pub/sub 的
        （如 `bridge`）得在这里自己重来一遍。
        """

    async def switch_room(self, room: str) -> None:
        """换房间 = 换 Zenoh key 前缀（`topics.PREFIX` + room + 后缀，见 `topics.py`）。

        ⚠️ 为什么不是"改个变量"：key 在**声明时**就绑死了（publisher / subscriber
        都持有具体的 key 表达式），房间一换就得把整套 pub/sub 重新声明一遍。

        ⚠️ 为什么不是"重跑 `setup()`"：各节点把 Publisher / Subscriber 存进了自己的
        属性（`self._pub_fire` 这种），重跑会生成新对象、旧引用全废。这里**保留 wrapper
        身份、只换里面的原生对象**，所以节点代码一行都不用改。

        ⚠️ `_switching` 是给**别的线程**看的护栏：窗口期内 session 正在重开，
        这时发消息必然失败，而失败会被 `_fatal` 判成"节点有未捕获异常"直接打死
        （device 的注册表轮询线程、sim_coyote 的 2s 轮询都可能正好撞上）——
        所以窗口期内的 put 直接丢弃（见 `Publisher.put`）。

        同步执行（不开线程）：整个过程没有 await，单线程事件循环里就是**原子**的，
        别的协程插不进来。
        """
        room = str(room or "").strip()
        if not room:
            raise NodeError("房间名不能为空")
        if room == self.room:
            return
        old, self.room = self.room, room
        self._switching = True
        try:
            self._release_declarations()
            session, self._session = self._session, None
            if session is not None:
                try:
                    session.close()
                except Exception as exc:  # noqa: BLE001 - 关旧 session 失败也不该打死节点
                    self.log.warning("换房间时关闭旧 session 失败：%s", exc)
            self._open_session()
            self._redeclare()
            self._status_publisher = None      # 心跳按新 room 重建
        finally:
            self._switching = False
        self.log.info(
            "房间已切换：%s → %s（重声明 %d 个 pub/sub）", old, room, len(self._registrations)
        )
        try:
            roomstate.write_room(self.config.data_dir, room)
        except Exception as exc:  # noqa: BLE001 - 落盘失败不影响这次切换
            self.log.debug("房间落盘失败：%s", exc)
        await self.on_room_switched(old, room)

    def _release_declarations(self) -> None:
        """撤销全部订阅 / 查询服务 / 发布者（**不关 session**）—— 换房间前先清干净。"""
        for queryable in self._queryables:
            queryable.undeclare()
        self._queryables.clear()
        for subscriber in self._subscribers:
            subscriber.undeclare()
        self._subscribers.clear()
        for publisher in self._publishers.values():
            publisher.undeclare()
        self._publishers.clear()

    def _redeclare(self) -> None:
        """按新 room 把声明过的 pub/sub/queryable 全部重新声明（wrapper 身份不变）。"""
        for registration in self._registrations:
            registration.declare(self._session)
            if isinstance(registration, Publisher):
                self._publishers[registration.key] = registration
            elif isinstance(registration, Subscriber):
                self._subscribers.append(registration)
            else:
                self._queryables.append(registration)

    # ──────────────────────────────────────────────────────────
    # 生命周期
    # ──────────────────────────────────────────────────────────
    async def run(self) -> None:
        """`setup()` → 阻塞直到 `stop()`（或收到 SIGINT / SIGTERM / SIGBREAK）。"""
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._stop_event = asyncio.Event()
        if self._stop_requested:
            self._stop_event.set()

        self._signals = self._install_signal_handlers()
        self.log.info(
            "节点 %s 启动（room=%s, pid=%d, 信号=%s）",
            self.name,
            self.room,
            os.getpid(),
            "/".join(self._signals) or "未注册",
        )
        try:
            self._open_session()
            try:
                self._subscribe_room_command()      # ★ 房间指令：各节点都要跟着换
                await self.setup()
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001 - **先记日志**（handler 还开着）再抛
                # ⚠️ 顺序很关键：finally 里的 _shutdown() 会 close_handlers()，
                #    之后的日志全部写不进文件。若只 _record() 再 raise，异常信息就只在
                #    stderr 上，日志文件里没有 traceback —— 这正是 F5 曾经失败的原因。
                #    所以必须在这里（handler 尚在时）用 logger 记一次。
                self._fatal(exc, where="setup")
                raise
            finally:
                self._started.set()

            interval = self._heartbeat_interval()
            if interval > 0:
                self._heartbeat_task = loop.create_task(self._heartbeat_loop(interval))
                self.log.debug("心跳间隔 %.1fs → status/%s", interval, self.name)

            self._start_stall_watchdog(loop)

            await self._wait_for_stop()
        finally:
            await self._shutdown()

    def _start_stall_watchdog(self, loop: asyncio.AbstractEventLoop) -> None:
        """起一个看门狗：事件循环**卡住**就把所有线程栈打进日志。

        为什么要它：实测撞过好几次"某个节点日志突然停住、心跳/命令全无，进程却还活着"
        （用管道启动导致写日志阻塞、zenoh session 卡死… 都是这一类）。
        以前只能猜；现在卡住超过 :data:`_STALL_AFTER_S` 秒就把每个线程的 Python 栈
        写进**该节点自己的日志**（不依赖 stderr 被谁接走），一眼能看出卡在哪个调用上。
        """
        self._loop_tick = time.monotonic()

        async def _ticker() -> None:
            while True:
                await asyncio.sleep(1.0)
                self._loop_tick = time.monotonic()      # 循环还活着就每秒刷新

        loop.create_task(_ticker())

        def _watch() -> None:
            last_dump = 0.0
            while not self._stop_requested:
                time.sleep(5.0)
                stalled = time.monotonic() - self._loop_tick
                if stalled < _STALL_AFTER_S:
                    continue
                now = time.monotonic()
                if now - last_dump < 60.0:                 # 别刷屏：一分钟最多一次
                    continue
                last_dump = now
                self.log.warning(
                    "⚠️ 事件循环卡住 %.1fs（心跳/命令都会停）—— 下面是所有线程的栈，"
                    "用来定位卡在哪：", stalled,
                )
                self._dump_stacks()

        threading.Thread(target=_watch, name=f"{self.name}-stall-watch", daemon=True).start()

    def _dump_stacks(self) -> None:
        """把当前所有线程的 Python 栈写进日志（配合 :meth:`_start_stall_watchdog`）。"""
        try:
            frames = sys._current_frames()
        except Exception:  # pragma: no cover - 极端环境
            return
        for thread in threading.enumerate():
            frame = frames.get(thread.ident or -1)
            if frame is None:
                continue
            stack = "".join(traceback.format_stack(frame))
            self.log.warning("线程 %s（%s）栈：\n%s", thread.ident, thread.name, stack)

    def stop(self) -> None:
        """请求退出（**幂等、可重入、线程安全**）。"""
        with self._state_lock:
            self._stop_requested = True
            event = self._stop_event
            loop = self._loop
        if event is None:
            return
        if loop is None or loop.is_closed():
            event.set()
            return
        try:
            # 用 call_soon_threadsafe（而不是直接 event.set()）：
            # 信号处理器在主线程里同步执行，直接 set 不会唤醒阻塞在 selector 上的
            # 事件循环（Windows Proactor 上尤其明显），call_soon_threadsafe 会写
            # self-pipe 唤醒它。
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:  # 循环已关闭
            event.set()

    @classmethod
    def main(cls, argv: Sequence[str] | None = None) -> int:
        """节点进程入口（同步）：`raise SystemExit(MyNode.main())`。

        返回进程退出码：0 正常，1 有未捕获异常，130 被 KeyboardInterrupt 打断。
        """
        force_utf8()
        parser = argparse.ArgumentParser(
            prog=f"python -m agent.nodes.{cls.name or '<name>'}",
            description=f"单独运行节点 {cls.__name__}（调试用；也可以交给 supervisor）",
        )
        parser.add_argument("--config", default=None, help="agent/config.toml 的路径")
        parser.add_argument("--room", default=None, help="覆盖 room")
        parser.add_argument("--log-level", default=None, help="覆盖日志级别")
        parser.add_argument("--log-dir", default=None, help="覆盖日志目录")
        args = parser.parse_args(argv)

        config = Config.load(args.config)
        if args.room:
            config.room = args.room
        if args.log_level:
            config.log_level = normalize_level(args.log_level)
        if args.log_dir:
            config.log_dir = Path(args.log_dir).resolve()
        return run_node(cls(config))

    # ──────────────────────────────────────────────────────────
    # 内部：session / 心跳 / 分发
    # ──────────────────────────────────────────────────────────
    def _open_session(self) -> None:
        config = zenoh.Config()
        for key, value in self.config.zenoh_json5().items():
            config.insert_json5(key, value)
        self._session = zenoh.open(config)

    def _heartbeat_interval(self) -> float:
        value = type(self).heartbeat_interval
        if value is None:
            value = self.config.heartbeat_interval_s
        return max(0.0, float(value))

    def _teardown_timeout(self) -> float:
        value = type(self).teardown_timeout
        if value is None:
            value = self.config.teardown_timeout_s
        return max(0.1, float(value))

    async def _heartbeat_loop(self, interval: float) -> None:
        while True:
            try:
                self.heartbeat()
            except Exception as exc:  # noqa: BLE001 - 心跳失败不致命，但必须可见
                self.log.warning("心跳发送失败：%s", exc)
            await asyncio.sleep(interval)

    async def _wait_for_stop(self) -> None:
        """等 `stop()`。

        为什么不是一句 `await event.wait()`：见 `stop()` 的注释 —— 信号处理器
        在主线程同步执行，某些事件循环实现不会因此醒来。这里用 0.2s 轮询兜底，
        保证 Ctrl+C **一定**会在 0.2s 内生效（代价可忽略）。
        """
        event = self._stop_event
        assert event is not None
        while not event.is_set():
            try:
                await asyncio.wait_for(event.wait(), timeout=self.stop_poll_interval)
            except asyncio.TimeoutError:
                continue

    def _install_signal_handlers(self) -> tuple[str, ...]:
        """注册 SIGINT / SIGTERM / SIGBREAK 三件套（只能在主线程）。

        旧处理器会被记下来，退出时**还原** —— 库不该永久劫持进程的信号处理
        （否则在 pytest / 宿主程序里跑一次节点就把别人的 Ctrl+C 抢了）。
        """
        if threading.current_thread() is not threading.main_thread():
            self.log.debug("非主线程运行，跳过信号注册（由宿主调用 stop()）")
            return ()
        installed: list[str] = []
        for signame in ("SIGINT", "SIGTERM", "SIGBREAK"):
            signum = getattr(signal, signame, None)
            if signum is None:  # SIGBREAK 只在 Windows 有
                continue
            try:
                self._previous_signals.setdefault(signum, signal.getsignal(signum))
                signal.signal(signum, self._on_signal)
            except (ValueError, OSError, RuntimeError) as exc:
                self.log.debug("无法注册 %s：%s", signame, exc)
            else:
                installed.append(signame)
        return tuple(installed)

    def _restore_signal_handlers(self) -> None:
        for signum, previous in self._previous_signals.items():
            with contextlib.suppress(ValueError, OSError, RuntimeError, TypeError):
                signal.signal(signum, previous)
        self._previous_signals.clear()

    def _on_signal(self, signum: int, _frame: Any) -> None:
        name = _signal_name(signum)
        if self._stop_requested or self._closing:
            self.log.warning("已在退出中，忽略重复信号 %s", name)
            return
        self.log.info("收到 %s，开始优雅退出", name)
        self.stop()

    def _handle_sample(
        self, key: str, sample: Any, handler: Callable[..., Any], with_key: bool
    ) -> None:
        try:
            actual = str(sample.key_expr)
        except Exception:  # noqa: BLE001
            actual = key
        data = bytes(sample.payload)
        try:
            msg = messages.from_bytes(data)
        except messages.MessageError as exc:
            # 🔒 任务书 §4.2.1：坏消息原样传 bytes + warning，绝不打死节点
            self.log.warning(
                "收到无法解码的消息 %s（%s），原样传 bytes（%d 字节）：%r",
                actual,
                exc,
                len(data),
                data[:120],
            )
            msg = data
        self._invoke(handler, actual, msg, with_key)

    def _handle_query(self, key: str, raw_query: Any, handler: Callable[..., Any]) -> None:
        query = Query(self, raw_query, self._reply_key_for(key, raw_query))

        if _is_async_callable(handler):
            loop = self._loop
            if loop is None or loop.is_closed():
                self._fatal(NodeError("事件循环未运行，异步 handler 无法调度"), where=f"查询 {key}")
                with contextlib.suppress(Exception):
                    query.reply_error({"error": "node event loop is not running"})
                return
            future = asyncio.run_coroutine_threadsafe(self._query_runner(handler, query), loop)
            future.add_done_callback(self._on_future_done)
            return

        try:
            result = handler(query)
        except Exception as exc:  # noqa: BLE001 - 必须外显
            self._fatal(exc, where=f"查询服务 {key}")
            with contextlib.suppress(Exception):
                if not query.replied:
                    query.reply_error({"error": f"{type(exc).__name__}: {exc}"})
            return
        if not query.replied:
            # 没有显式 reply 也要回一次（空 body），否则查询方要白等到 timeout
            query.reply(result)

    async def _query_runner(self, handler: Callable[..., Any], query: Query) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            result = await handler(query)
            if not query.replied:
                query.reply(result)
        except Exception as exc:  # noqa: BLE001
            self._fatal(exc, where=f"查询服务 {query.key}")
            with contextlib.suppress(Exception):
                if not query.replied:
                    query.reply_error({"error": f"{type(exc).__name__}: {exc}"})
        finally:
            if task is not None:
                self._tasks.discard(task)

    def _reply_key_for(self, queryable_key: str, raw_query: Any) -> str:
        """挑一个具体（不含通配符）的回复 key。"""
        try:
            asked = str(raw_query.key_expr)
        except Exception:  # noqa: BLE001
            asked = queryable_key
        if topics.WILDCARD_ALL not in asked and topics.WILDCARD_ONE not in asked:
            return asked
        if (
            topics.WILDCARD_ALL not in queryable_key
            and topics.WILDCARD_ONE not in queryable_key
        ):
            return queryable_key
        return asked

    def _invoke(
        self, handler: Callable[..., Any], key: str, msg: Any, with_key: bool
    ) -> None:
        """在 Zenoh 的回调线程里把消息交给 handler（必要时桥接到事件循环）。"""
        args = (key, msg) if with_key else (msg,)
        where = f"订阅回调 {key}"

        if _is_async_callable(handler):
            loop = self._loop
            if loop is None or loop.is_closed():
                self._fatal(NodeError("事件循环未运行，异步 handler 无法调度"), where=where)
                return
            future = asyncio.run_coroutine_threadsafe(self._sub_runner(handler, args), loop)
            future.add_done_callback(self._on_future_done)
            return

        try:
            handler(*args)
        except Exception as exc:  # noqa: BLE001 - 必须外显（记日志 + 退出码非 0）
            self._fatal(exc, where=where)

    async def _sub_runner(self, handler: Callable[..., Any], args: tuple[Any, ...]) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            await handler(*args)
        finally:
            if task is not None:
                self._tasks.discard(task)

    def _on_future_done(self, future: Any) -> None:
        if future.cancelled():
            return
        exc = future.exception()
        if exc is not None:
            self._fatal(exc, where="异步回调")

    # ──────────────────────────────────────────────────────────
    # 内部：错误与清理
    # ──────────────────────────────────────────────────────────
    def _record(self, exc: BaseException) -> None:
        if self._error is None:
            self._error = exc

    def _fatal(self, exc: BaseException, *, where: str = "") -> None:
        """记日志 + 请求退出。

        `_error` 被设置后 `run_node()` 会以**非 0 退出码**结束进程，
        让 supervisor 按退避重启 —— 这就是"异常必须外显"的落地（任务书 §4.2 要求 2）。
        """
        if self._closing:
            self.log.debug("退出过程中忽略 %s 的异常：%s", where or "运行时", exc)
            return
        self.log.error("节点 %s 未捕获异常（%s）：%s", self.name, where or "运行时", exc, exc_info=exc)
        self._record(exc)
        self.stop()

    async def _shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._closing = True
        self.log.info("节点 %s 开始清理", self.name)

        # 1) 用户 teardown（超时也要继续清理，绝不卡死退出）
        if not self._teardown_called:
            self._teardown_called = True
            timeout = self._teardown_timeout()
            try:
                await asyncio.wait_for(self.teardown(), timeout=timeout)
            except asyncio.TimeoutError:
                self.log.error("teardown() 超过 %.1fs 未返回，强制继续清理", timeout)
            except asyncio.CancelledError:
                self.log.warning("teardown() 被取消")
            except Exception as exc:  # noqa: BLE001
                self._record(exc)
                self.log.error("teardown() 抛异常", exc_info=exc)

        # 2) 停心跳（免得它往马上要撤销的 publisher 上写）
        heartbeat_task = self._heartbeat_task
        self._heartbeat_task = None
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat_task

        # 3) 先撤订阅/查询服务，不再有新的回调进来
        for queryable in self._queryables:
            queryable.undeclare()
        self._queryables.clear()
        for subscriber in self._subscribers:
            subscriber.undeclare()
        self._subscribers.clear()

        # 4) 收尾在途的异步回调
        pending = [task for task in self._tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

        # 5) 撤发布者 + 关 session（不留残留 Zenoh session）
        status_publisher = self._status_publisher
        self._status_publisher = None
        if status_publisher is not None:
            self._publishers.pop(status_publisher.key, None)
            status_publisher.undeclare()
        for publisher in self._publishers.values():
            publisher.undeclare()
        self._publishers.clear()

        session = self._session
        self._session = None
        if session is not None:
            try:
                session.close()
            except Exception as exc:  # noqa: BLE001
                self._record(exc)
                self.log.error("session.close() 失败", exc_info=exc)
            else:
                self.log.debug("Zenoh session 已关闭")

        # 6) 还原进程原有的信号处理器（别劫持宿主）
        self._restore_signal_handlers()

        # 7) 最后一条日志先写完。
        #    ⚠️ 这里**故意不关**日志 handler —— 退出码（如 "exit=1"）是在
        #    run_node() 里才确定的；若此处就 close_handlers()，那条关键记录会
        #    **静默丢失**（不报错、不抛异常），日志文件里就查不到退出码。
        #    统由 run_node() 在所有日志写完后关闭（Windows 上不释放句柄就删不掉
        #    日志目录，所以这一步不能省，只是移到最后一刻）。
        self.log.info("节点 %s 已退出（session 已关闭）", self.name)


# ──────────────────────────────────────────────────────────────
# 同步入口
# ──────────────────────────────────────────────────────────────
def run_node(node: Node) -> int:
    """阻塞运行 `node`，返回**进程退出码**。

    - 正常退出（含 SIGINT/SIGTERM/SIGBREAK 优雅退出）→ `0`
    - 未捕获异常 / handler 出错 → `1`（日志里有完整 traceback）
    - `KeyboardInterrupt`（未走优雅路径）→ `130`
    本函数是**日志 handler 的唯一关闭点** —— 退出码相关的日志都发生在这里，
    必须在全部写完之后再 close（见 `_shutdown()` 步骤 7 的说明）。
    """
    force_utf8()
    try:
        try:
            asyncio.run(node.run())
        except KeyboardInterrupt:
            node.log.warning("节点 %s 被 KeyboardInterrupt 中断（exit=130）", node.name)
            return 130
        except BaseException as exc:  # noqa: BLE001 - 顶层必须外显
            node.log.critical(
                "节点 %s 因未捕获异常退出（exit=1）", node.name, exc_info=exc
            )
            return 1

        if node.error is not None:
            node.log.critical("节点 %s 退出码 1（原因：%s）", node.name, node.error)
            return 1
        node.log.info("节点 %s 正常退出（exit=0）", node.name)
        return 0
    finally:
        # ★ 所有日志都写完了才关 handler。
        #   提前关会让 "exit=1" / traceback 这类关键记录静默丢失（F5 踩过）。
        close_handlers(node.log)
