"""消息 schema（dataclass + JSON）。

来源：`docs/tasks/TF-节点框架与消息总线.md` §4.3。

契约（任务书 §4.3 三条规则）：

1. 本任务**精确实现这 4 个消息** + `Message` 基类 + `TOPIC_MESSAGES` 注册表；
2. 其余 8 个 key 的 payload 由各自节点任务（T2 / T4 / T7）定义，并**注册进 `TOPIC_MESSAGES`**；
3. 🔒 **字段只增不改**：可以新增字段（向后兼容），但**改已有字段名或语义必须先回报**
   —— 否则会静默破坏别的节点，而这种 bug 最难查。

序列化统一走 JSON + `ensure_ascii=False` + UTF-8（任务书 §4.2 要求 4，可调试性优先）。
`tests/test_messages.py` 里有一条"字段名集合"的哨兵测试守着规则 3。
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from typing import Any, ClassVar

from . import topics

logger = logging.getLogger("coyotelink.messages")

__all__ = [
    "MessageError",
    "Message",
    "FeaturePose",
    "FeatureFace",
    "FeatureHeart",
    "CommandFire",
    "EventFire",
    "TOPIC_MESSAGES",
    "message_class",
    "to_bytes",
    "from_bytes",
]


class MessageError(ValueError):
    """payload 与 schema 对不上（解码失败 / 缺字段 / 类型不符）。"""


# ──────────────────────────────────────────────────────────────
# 通用编解码
# ──────────────────────────────────────────────────────────────
def to_bytes(payload: Any) -> bytes:
    """把任意 payload 序列化成 UTF-8 bytes。

    - `None`            → `b""`
    - `bytes`/`bytearray`/`memoryview` → 原样
    - `str`             → **裸 UTF-8 文本**（不当成 JSON 再包一层引号，
      这样 `cli pub <key> hello` 发出去的就是 `hello`）
    - `Message`/dataclass → `ensure_ascii=False` 的 JSON
    - `dict`/`list`/标量  → `ensure_ascii=False` 的 JSON

    ⚠️ 与 `from_bytes()` 有意不对称：收方向来按 JSON 解析，所以裸文本会在
    订阅侧降级成 `bytes` + warning（任务书 §4.2.1）。业务消息请用
    `Message` / `dict`，`str` 是给调试和纯文本场景留的。
    """
    if payload is None:
        return b""
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode("utf-8")
    if isinstance(payload, Message):
        return payload.to_bytes()
    if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
        payload = dataclasses.asdict(payload)
    try:
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MessageError(
            f"payload 无法序列化为 JSON（类型 {type(payload).__name__}）：{exc}"
        ) from exc


def from_bytes(data: bytes | bytearray | memoryview) -> Any:
    """UTF-8 JSON bytes → Python 对象。

    失败一律抛 `MessageError`（**由调用方决定是否降级**：订阅侧会降级成
    原样 `bytes` + warning，见任务书 §4.2.1「坏消息不能打死节点」）。
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise MessageError(f"payload 必须是 bytes，得到 {type(data).__name__}")
    raw = bytes(data)
    if not raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MessageError(f"payload 不是合法 UTF-8：{exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MessageError(f"payload 不是合法 JSON：{exc}") from exc


# ──────────────────────────────────────────────────────────────
# 基类
# ──────────────────────────────────────────────────────────────
@dataclass
class Message:
    """所有消息 dataclass 的基类。只用标准库（不强制 pydantic）。"""

    #: 本消息对应的 topics 后缀常量（子类覆盖，用于 `TOPIC_MESSAGES` 自检）
    topic: ClassVar[str] = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def to_bytes(self) -> bytes:
        return self.to_json().encode("utf-8")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        """从 dict 构造。

        - **未知字段忽略**（向后兼容：新版本发的消息老版本也能读）
        - 缺必需字段 → `MessageError`，并列出缺了哪些
        """
        if not isinstance(data, dict):
            raise MessageError(f"{cls.__name__} 需要 JSON 对象，得到 {type(data).__name__}")
        names = {f.name for f in dataclasses.fields(cls)}
        known = {k: v for k, v in data.items() if k in names}
        unknown = sorted(set(data) - names)
        if unknown:
            logger.debug("%s 忽略未知字段：%s", cls.__name__, ", ".join(unknown))
        missing = sorted(
            f.name
            for f in dataclasses.fields(cls)
            if f.name not in known
            and f.default is dataclasses.MISSING
            and f.default_factory is dataclasses.MISSING  # type: ignore[misc]
        )
        if missing:
            raise MessageError(f"{cls.__name__} 缺少必需字段：{', '.join(missing)}")
        try:
            return cls(**known)  # type: ignore[return-value]
        except TypeError as exc:
            raise MessageError(f"{cls.__name__} 字段类型不符：{exc}") from exc

    @classmethod
    def from_json(cls, text: str | bytes) -> "Message":
        if isinstance(text, (bytes, bytearray, memoryview)):
            text = bytes(text).decode("utf-8")
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MessageError(f"{cls.__name__} JSON 解析失败：{exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def from_bytes(cls, data: bytes | bytearray | memoryview) -> "Message":
        payload = from_bytes(data)
        if payload is None:
            raise MessageError(f"{cls.__name__} 收到空 payload，无法解析")
        return cls.from_dict(payload)


# ──────────────────────────────────────────────────────────────
# §4.3 的 4 个消息（🔒 字段只增不改）
# ──────────────────────────────────────────────────────────────
@dataclass
class FeaturePose(Message):
    """`feature/pose` —— 姿态 33 点（由 pose 节点发布）。

    `ts` 用**毫秒**时间戳（与 `docs/架构设计与路线图.md` §5.7 一致）。
    """

    topic: ClassVar[str] = topics.FEATURE_POSE

    ts: float
    seq: int
    present: bool
    landmarks: list[list[float]]  # 33×3，归一化 0~1
    visibility: list[float] | None = None


@dataclass
class FeatureFace(Message):
    """`feature/face` —— 表情 + 头姿（由 face 节点发布）。"""

    topic: ClassVar[str] = topics.FEATURE_FACE

    ts: float
    emotion: str | None
    confidence: float | None
    head: dict | None  # {"yaw":.., "pitch":.., "roll":..}


@dataclass
class CommandFire(Message):
    """`command/fire` —— 开火指令（rules / web 发布，coyote_v4 订阅）。"""

    topic: ClassVar[str] = topics.COMMAND_FIRE

    waveform: str
    channel: str = "A"
    duration_ms: int = 2000
    priority: int = 1
    overlay: bool = True  # True → 叠加（不抢占）
    # “一键开火”的强度部分（二选一；都不给 = 只换波形不调强度）
    intensity_delta: int | None = None  # 相对增加，如 10
    intensity: int | None = None        # 绝对设定，0~200


@dataclass
class EventFire(Message):
    """`event/fire` —— 开火审计记录（coyote_v4 发布）。"""

    topic: ClassVar[str] = topics.EVENT_FIRE

    ts: float
    source: str  # "rules" | "web"
    waveform: str
    channel: str
    duration_ms: int
    # —— 可选审计补充字段（**只增不改**，默认 None 保持向后兼容）——
    # 排障时"开了什么波形 / 多久 / 多少帧 / 叠没叠加 / 打到哪台设备"都是依据。
    version: int | None = None
    frame_count: int | None = None
    priority: int | None = None
    overlay: bool | None = None
    client_id: str | None = None
    slot_id: str | None = None
    #: 这次开火到底成没成（None = 老版本消息，按成功处理）。
    #: ★ 为什么要有它：面板点「开火」时 web 节点只回了一句"收到了"，
    #:   真正的失败（例如"没有已接入的 App，无法开火"）以前只在 coyote_v4 的日志里 ——
    #:   用户看到"后端已受理"却什么都没发生（实测就是这么被投诉的）。
    ok: bool | None = None
    #: `ok=False` 时的原因（人话，面板直接显示）。
    error: str | None = None
    #: **谁**发起的（主机名）：被控端视图要显示"哪个机器在控制我"（房间内跨机时尤其重要）。
    by: str | None = None
    #: 打给了谁（`local` / `all` / 主机名）—— 被控端据此判断"这一炮是不是打我的"。
    target: str | None = None


@dataclass
class FeatureHeart(Message):
    """`feature/heart` —— 心率（由 heart 节点发布，T7）。

    `ts` 用**毫秒**（与 `FeaturePose` 一致）。
    """

    topic: ClassVar[str] = topics.FEATURE_HEART

    ts: float
    bpm: int
    contact: bool | None = None       # 皮肤接触检测（设备支持时才有）
    rr_ms: list[float] | None = None  # RR 间期（毫秒）
    device: str = ""                  # 设备名
    mac: str = ""                     # 设备地址


#: 后缀 → 消息类。**新增 key 时在这里注册**（任务书 §4.3 规则 2）。
TOPIC_MESSAGES: dict[str, type[Message]] = {
    topics.FEATURE_POSE: FeaturePose,
    topics.FEATURE_FACE: FeatureFace,
    topics.FEATURE_HEART: FeatureHeart,
    topics.COMMAND_FIRE: CommandFire,
    topics.EVENT_FIRE: EventFire,
}


def message_class(suffix_or_key: str) -> type[Message] | None:
    """按 key（或后缀）查消息类；没注册返回 None（不是错误）。"""
    suffix = topics.suffix_of(suffix_or_key) if topics.is_full_key(suffix_or_key) else suffix_or_key
    return TOPIC_MESSAGES.get(suffix)


# 自检：注册表里的 key 必须都是 topics 定义过的后缀，避免打错字
_unknown = sorted(k for k in TOPIC_MESSAGES if k not in topics.ALL_SUFFIXES)
if _unknown:  # pragma: no cover - 只在改错 topics 时触发
    raise RuntimeError(f"TOPIC_MESSAGES 注册了 topics.py 未定义的后缀：{_unknown}")

_undeclared = sorted(
    name for name, cls in TOPIC_MESSAGES.items() if getattr(cls, "topic", "") != name
)
if _undeclared:  # pragma: no cover
    raise RuntimeError(f"TOPIC_MESSAGES 与 Message.topic 不一致：{_undeclared}")
