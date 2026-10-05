"""「开火」高层语义 —— 把一条 ``command/fire`` 变成一次真实的 ``send_pulse``。

任务书 T4 §6「``fire()`` 的语义」（需求原文：惩罚 = 特定波形开火）：

1. 找目标 ``client_id``（默认最近接入的那个）与 ``slot_id``（来自设备发现）；
2. 取帧：从波形查找表拿 :class:`~agent.nodes.coyote_v4.waveform_io.Waveform`；
3. ``duration_ms`` 与帧自然长度不符 → **循环补齐 / 截断**（SDK 不做，这是本模块的活）；
4. 调 ``send_pulse(...)`` 下发；
5. ``options`` 带 ``priority`` 与 ``immediate``（``overlay=True`` → ``immediate=False`` 叠加不抢占）。

强度语义（§6.2 —— 真机反馈，本模块的**核心**）
----------------------------------------------

郊狼的输出 = **强度 × 波形**：光发波形而通道强度为 0，几乎没感觉。真实玩法是：

- **一次开火 = 临时提强度 + 播波形，时长到了强度回落**（否则越堆越高）；
- **强度以 App 回报为准**：App 会限幅（发 ``+10`` 可能只生效 ``+3``），本地只做缓存，
  开火时**只读缓存**，不轮询（§5.9.4）；
- 开火前用「**最近已知真实强度 + 目标值**」对照「**当前上限**」；上限是**运行时状态**，
  从事件里读，**不硬编码**（§5.9.3）。

落地方案：

============================  =========================================================
``intensity_mode="temp"``     用 ``set_temp_intensity(v, duration)``（V4 动作码 ``SetTempIntensity=4``）：
                              **App 侧自动回落**，无需我们守时。默认方式。
``intensity_mode="add"``      回退方案：自己 ``add_intensity(+N)``，``duration`` 后 ``add_intensity(-N)``。
============================  =========================================================

无论哪种方式，都挂一个**看门狗**：``duration + grace`` 后若发现强度**没有回落**，
强制 ``reset_intensity`` 兜底（防网络抖动/掉线导致降不回去）。

本模块**只依赖**一个鸭子类型的 ``client``：``default_client_id`` / ``default_slot_id`` /
``send_pulse`` / ``add_intensity`` / ``set_temp_intensity`` / ``reset_intensity`` /
``intensity_strength`` / ``intensity_limit``。缺失的方法会被优雅降级，因此单测可用极简假 client，
无需 SDK、无需联网。

与 SDK 的字段对应关系（读 ``socket/v4.py`` 的 ``_create_operate_base`` 确认）：

==============================  ==================================
本模块的 ``options`` 键           SDK 写出的 payload 字段
==============================  ==================================
``priority``                    ``p``
``immediate``                   ``im``
``version``                     ``ver``（仅 ``send_pulse``）
``seq``                         ``seq``（仅 ``send_pulse``）
==============================  ==================================
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

from . import waveform_io
from .client import ClientError, channel_int
from .intensity import HARD_MAX_INTENSITY
from .waveform_io import Waveform

__all__ = [
    "FireError",
    "FireRequest",
    "FireResult",
    "VALID_PRIORITIES",
    "INTENSITY_MODE_TEMP",
    "INTENSITY_MODE_ADD",
    "DEFAULT_WATCHDOG_GRACE_MS",
    "build_options",
    "fire",
]

logger = logging.getLogger("coyotelink.coyote_v4.fire")

#: SDK 只接受这三档优先级（``V4OperateOptions.priority: Literal[0, 1, 2]``）
VALID_PRIORITIES = (0, 1, 2)

#: 强度施加方式：``temp`` = App 自动回落（首选）；``add`` = 自己定时降回（回退）。
INTENSITY_MODE_TEMP = "temp"
INTENSITY_MODE_ADD = "add"

#: 看门狗默认宽限（ms）：等 App 的回落事件到达的额外时间。
DEFAULT_WATCHDOG_GRACE_MS = 700


class FireError(RuntimeError):
    """开火无法完成（波形不存在、没有可用 App / 设备、参数非法……）。"""


@dataclass
class FireRequest:
    """一次开火的完整意图。

    ``channel`` 用 ``"A"`` / ``"B"``（大小写不敏感），或直接给 ``0`` / ``1``。
    ``version`` 为 ``None`` 时用波形自身的版本；显式给 2/3 可强制（例如把 V3 波形
    按 V2 下发 —— 一般不该这么做，仅在调试用）。
    """

    waveform: str
    channel: str = "A"
    duration_ms: int = 2000
    priority: int = 1
    overlay: bool = True  # True → 叠加（不抢占）
    client_id: str | None = None
    slot_id: str | None = None
    seq: int | None = None
    version: int | None = None
    # ── “一键开火”的核心：强度 ────────────────────────────────
    # 郊狼的输出 = 强度 × 波形。**光发波形、强度为 0 = 基本没感觉**。
    # 下面两个二选一（或都不给，纯换波形）：
    intensity_delta: int | None = None  # 相对增加（最常用，如 +10）
    intensity: int | None = None        # 绝对设定到某值（0~200）
    # ── §6.2 强度语义 ─────────────────────────────────────────
    intensity_mode: str = INTENSITY_MODE_TEMP  # "temp"（App 自动回落）| "add"（定时降回）
    intensity_duration_ms: int | None = None   # 临时强度时长；默认 = duration_ms
    watchdog_grace_ms: int = DEFAULT_WATCHDOG_GRACE_MS  # 回落看门狗宽限（ms）

    @classmethod
    def from_command(cls, data: Any) -> "FireRequest":
        """从 ``command/fire`` 的 payload（dict / dataclass）构造。

        只认本 dataclass 的字段；未知字段忽略（与 :class:`~agent.core.messages.Message`
        的向后兼容约定一致）。缺 ``waveform`` → :class:`FireError`。
        """
        if data is None:
            raise FireError("开火指令为空")
        if hasattr(data, "to_dict"):
            data = data.to_dict()
        if not isinstance(data, Mapping):
            raise FireError(f"开火指令必须是对象，得到 {type(data).__name__}")
        if "waveform" not in data or not str(data.get("waveform") or "").strip():
            raise FireError("开火指令缺少 waveform")
        kwargs: dict[str, Any] = {}
        for name in (
            "channel", "duration_ms", "priority", "overlay", "client_id", "slot_id",
            "seq", "version", "intensity_delta", "intensity",
            "intensity_mode", "intensity_duration_ms", "watchdog_grace_ms",
        ):
            if name in data and data[name] is not None:
                kwargs[name] = data[name]
        return cls(waveform=str(data["waveform"]).strip(), **kwargs)


@dataclass
class FireResult:
    """一次开火的落地结果（供审计 ``event/fire`` 与日志）。"""

    waveform: str
    version: int
    channel: str
    duration_ms: int
    frame_count: int
    priority: int
    overlay: bool
    client_id: str
    slot_id: str
    seq: int | None = None
    aliases: tuple[str, ...] = field(default_factory=tuple)
    intensity_delta: int | None = None
    intensity: int | None = None
    intensity_after: int | None = None  # 本次实际施加的目标值（信息用）
    # ── §6.2 强度语义（审计/排障用）──────────────────────────
    intensity_mode: str | None = None       # "temp" | "add" | None（没动强度）
    intensity_before: int | None = None     # 开火前 App 回报的真实强度（可能 None）
    intensity_requested: int | None = None  # 限幅后**实际发出**的目标强度
    intensity_limit: int | None = None      # 当时的上限（None = 还没收到过）
    intensity_clamped: bool = False         # 是否被上限/协议边界限幅
    watchdog_scheduled: bool = False        # 是否挂了回落看门狗

    @property
    def immediate(self) -> bool:
        """是否抢占（``overlay=False`` → True）。"""
        return not self.overlay

    def to_dict(self) -> dict[str, Any]:
        return {
            "waveform": self.waveform,
            "version": self.version,
            "channel": self.channel,
            "duration_ms": self.duration_ms,
            "frame_count": self.frame_count,
            "priority": self.priority,
            "overlay": self.overlay,
            "client_id": self.client_id,
            "slot_id": self.slot_id,
            "seq": self.seq,
            "intensity_delta": self.intensity_delta,
            "intensity": self.intensity,
            "intensity_mode": self.intensity_mode,
            "intensity_before": self.intensity_before,
            "intensity_requested": self.intensity_requested,
            "intensity_limit": self.intensity_limit,
            "intensity_clamped": self.intensity_clamped,
            "watchdog_scheduled": self.watchdog_scheduled,
        }


def _normalize_channel(channel: Any) -> str:
    """通道 → 规范名 ``"A"`` / ``"B"``（非法通道统一转成 :class:`FireError`）。"""
    try:
        value = channel_int(channel)
    except ClientError as exc:
        raise FireError(str(exc)) from exc
    return "A" if value == 0 else "B"


def _normalize_duration(duration_ms: Any) -> int:
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int):
        raise FireError(f"duration_ms 必须是整数，得到 {duration_ms!r}")
    if duration_ms <= 0:
        raise FireError(f"duration_ms 必须为正数，得到 {duration_ms}")
    return duration_ms


def _normalize_intensity(value: Any, *, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FireError(f"{what} 必须是整数，得到 {value!r}")
    if not (0 <= value <= HARD_MAX_INTENSITY):
        raise FireError(f"{what} 超出范围 0~{HARD_MAX_INTENSITY}，得到 {value}")
    return value


def _normalize_priority(priority: Any) -> int:
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise FireError(f"priority 必须是整数，得到 {priority!r}")
    if priority not in VALID_PRIORITIES:
        raise FireError(f"priority 只能是 {VALID_PRIORITIES}，得到 {priority}")
    return priority


def build_options(request: FireRequest, version: int) -> dict[str, Any]:
    """组装 SDK ``send_pulse`` 的 ``options``。

    - ``overlay=True``（默认）→ ``immediate=False``（叠加，不抢占对端已有任务）；
    - ``version``：V2 帧传 ``2``，V3 帧传 ``3``（任务书 §4.6.6：省略或 3 都表示 V3）；
    - ``seq`` / ``priority`` 按需带上。
    """
    if version not in (2, 3):
        raise FireError(f"波形版本只能是 2 或 3，得到 {version}")
    options: dict[str, Any] = {
        "priority": _normalize_priority(request.priority),
        "immediate": not request.overlay,
        "version": version,
    }
    if request.seq is not None:
        options["seq"] = int(request.seq)
    return options


def _resolve_waveform(table: Mapping[str, Waveform], name: str) -> Waveform:
    wave = waveform_io.find(dict(table), name)
    if wave is None:
        available = ", ".join(sorted(table)[:8])
        raise FireError(
            f"找不到波形 {name!r}；已加载 {len(table)} 条（示例：{available or '空'}）"
        )
    return wave


# ──────────────────────────────────────────────────────────────
# 强度语义（§6.2）
# ──────────────────────────────────────────────────────────────
def _has_method(obj: Any, name: str) -> bool:
    return callable(getattr(obj, name, None))


def _read_strength(client: Any, client_id: str, slot_id: str, channel: str) -> int | None:
    """最近已知真实强度（读缓存，**不 query**）。client 不支持则 None。"""
    getter = getattr(client, "intensity_strength", None)
    if not callable(getter):
        return None
    try:
        value = getter(client_id, slot_id, channel)
    except Exception as exc:  # noqa: BLE001 - 缓存读取失败不该打死开火
        logger.debug("读强度缓存失败：%s", exc)
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _read_limit(client: Any, client_id: str, slot_id: str, channel: str) -> int | None:
    """当上限（读缓存）。client 不支持则 None。"""
    getter = getattr(client, "intensity_limit", None)
    if not callable(getter):
        return None
    try:
        value = getter(client_id, slot_id, channel)
    except Exception as exc:  # noqa: BLE001
        logger.debug("读上限缓存失败：%s", exc)
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass
class _IntensityPlan:
    """本次开火的强度落地计划（内部）。"""

    mode: str          # "temp" | "add"
    requested: int     # 限幅后实际发出的目标强度
    before: int | None
    limit: int | None
    clamped: bool
    delta: int         # 相对开火前的增量（add 模式用来回落）
    duration_ms: int   # 临时强度时长（= 回落时点）


def _plan_intensity(
    client: Any,
    client_id: str,
    slot_id: str,
    channel: str,
    request: FireRequest,
    duration: int,
) -> _IntensityPlan | None:
    """把「意图强度」+「缓存真实值」+「缓存上限」算成一份落地计划。"""
    if request.intensity is None and request.intensity_delta is None:
        return None  # 纯换波形，不碰强度

    mode = str(request.intensity_mode or INTENSITY_MODE_TEMP).strip().lower()
    if mode not in (INTENSITY_MODE_TEMP, INTENSITY_MODE_ADD):
        raise FireError(f"intensity_mode 只能是 'temp' 或 'add'，得到 {request.intensity_mode!r}")
    if mode == INTENSITY_MODE_TEMP and not _has_method(client, "set_temp_intensity"):
        # 回退：client 没提供临时强度 → 自己 add + 定时降回
        mode = INTENSITY_MODE_ADD

    before = _read_strength(client, client_id, slot_id, channel)
    if request.intensity is not None:
        target = _normalize_intensity(request.intensity, what="intensity")
    else:
        delta = _normalize_intensity(request.intensity_delta, what="intensity_delta")
        target = (before or 0) + delta

    # 限幅：先协议值域，再 App 的运行时上限（§5.9.3，不硬编码）
    limit = _read_limit(client, client_id, slot_id, channel)
    clamped = False
    if target > HARD_MAX_INTENSITY:
        target, clamped = HARD_MAX_INTENSITY, True
    if limit is not None and target > limit:
        target, clamped = limit, True

    temp_duration = duration if request.intensity_duration_ms is None else _normalize_duration(
        request.intensity_duration_ms
    )
    return _IntensityPlan(
        mode=mode,
        requested=target,
        before=before,
        limit=limit,
        clamped=clamped,
        delta=target - (before or 0),
        duration_ms=temp_duration,
    )


async def _apply_intensity(
    client: Any, client_id: str, slot_id: str, channel: str, plan: _IntensityPlan
) -> None:
    """下发强度操作。

    **不携带** ``priority`` / ``immediate``：强度只是“临时抬高”，若带上默认的
    ``im=False``（叠加），某些 App 会把它当成可排队任务而排到波形之后（真机反馈：
    不传 options 即立即生效）。因此与旧的可用实现保持一致，强度操作不带 options。
    """
    if plan.mode == INTENSITY_MODE_TEMP:
        # App 侧自动回落：到 plan.duration_ms 后强度自己降回去
        await client.set_temp_intensity(
            client_id, slot_id, channel, plan.requested, plan.duration_ms
        )
    elif plan.delta != 0:
        await client.add_intensity(client_id, slot_id, channel, plan.delta)


async def _force_reset(client: Any, client_id: str, slot_id: str, channel: str) -> None:
    reset = getattr(client, "reset_intensity", None)
    if callable(reset):
        await reset(client_id, slot_id, channel)
    else:
        logger.warning("强度看门狗想强制归零，但 client 没有 reset_intensity 可用")


async def _intensity_watchdog(
    client: Any, client_id: str, slot_id: str, channel: str, plan: _IntensityPlan, delay_s: float
) -> None:
    """``duration + grace`` 后兜底：temp 模式确认回落，add 模式主动降回（§6.2 看门狗）。"""
    try:
        await asyncio.sleep(delay_s)
        base = plan.before if plan.before is not None else 0
        if plan.mode == INTENSITY_MODE_TEMP:
            current = _read_strength(client, client_id, slot_id, channel)
            if current is not None and current > base:
                logger.warning(
                    "强度看门狗：临时强度未回落（当前=%s，开火前=%s），强制归零", current, base
                )
                await _force_reset(client, client_id, slot_id, channel)
        elif plan.delta:
            logger.info("强度看门狗：把强度降回（%+d）", -plan.delta)
            await client.add_intensity(client_id, slot_id, channel, -plan.delta, None)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 看门狗失败不能影响主流程
        logger.warning("强度回落看门狗异常：%s", exc)


def _schedule_watchdog(
    client: Any, client_id: str, slot_id: str, channel: str, plan: _IntensityPlan, grace_ms: int
) -> bool:
    """把看门狗挂到事件循环（无运行中的循环则跳过）。返回是否挂上。"""
    delay_s = max(0.0, (plan.duration_ms + max(0, int(grace_ms))) / 1000.0)
    coro = _intensity_watchdog(client, client_id, slot_id, channel, plan, delay_s)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return False
    loop.create_task(coro)
    return True


async def fire(client: Any, table: Mapping[str, Waveform], request: FireRequest) -> FireResult:
    """执行一次开火。

    :param client: 鸭子类型 —— 需有 ``default_client_id``、``default_slot_id(cid)``、
        ``send_pulse(cid, slot_id, channel, duration_ms, frames, options)``（见 :class:`CoyoteClient`）；
        强度相关方法（``set_temp_intensity`` / ``add_intensity`` / ``reset_intensity`` /
        ``intensity_strength`` / ``intensity_limit``）可选，缺失则优雅降级。
    :param table: 波形查找表（``name → Waveform``，来自 ``waveform_io.load_all``）
    :param request: 开火意图
    :returns: :class:`FireResult`（审计用）
    :raises FireError: 波形不存在、没有可用 App/设备、参数非法
    """
    duration = _normalize_duration(request.duration_ms)
    channel = _normalize_channel(request.channel)
    wave = _resolve_waveform(table, request.waveform)
    version = wave.version if request.version is None else int(request.version)
    if version not in (2, 3):
        raise FireError(f"波形版本只能是 2 或 3，得到 {version}")

    frames = waveform_io.frames_for_duration(wave, duration)

    client_id = request.client_id or client.default_client_id
    if not client_id:
        raise FireError("没有已接入的 App，无法开火")
    slot_id = request.slot_id or client.default_slot_id(client_id)
    if not slot_id:
        raise FireError(f"client={client_id} 没有可用设备（还没发现 slot_id）")

    options = build_options(request, version)

    # ★ §6.2 一键开火的核心：**先把强度临时提上去**，时长到了回落；再播波形。
    #   强度永远以 App 回报为准（读缓存），不 query、不假定意图值生效。
    plan = _plan_intensity(client, client_id, slot_id, channel, request, duration)
    if plan is not None:
        if plan.limit is not None and plan.clamped:
            logger.info(
                "强度按上限限幅：目标 %s（上限 %s，开火前真实 %s）",
                plan.requested, plan.limit, plan.before,
            )
        await _apply_intensity(client, client_id, slot_id, channel, plan)
        logger.info(
            "强度：通道%s 方式=%s 开火前=%s → 目标=%s（上限=%s%s）",
            channel, plan.mode, plan.before, plan.requested, plan.limit,
            "，已限幅" if plan.clamped else "",
        )

    logger.info(
        "开火：波形=%s(v%d) 通道=%s 时长=%dms 帧=%d client=%s slot=%s priority=%d %s",
        wave.name,
        version,
        channel,
        duration,
        len(frames),
        client_id,
        slot_id,
        options["priority"],
        "叠加" if request.overlay else "抢占",
    )
    await client.send_pulse(client_id, slot_id, channel, duration, frames, options)

    watchdog = False
    if plan is not None:
        watchdog = _schedule_watchdog(
            client, client_id, slot_id, channel, plan, request.watchdog_grace_ms
        )

    return FireResult(
        waveform=wave.name,
        version=version,
        channel=channel,
        duration_ms=duration,
        frame_count=len(frames),
        priority=options["priority"],
        overlay=request.overlay,
        client_id=client_id,
        slot_id=slot_id,
        seq=request.seq,
        aliases=tuple(wave.aliases),
        intensity_delta=request.intensity_delta,
        intensity=request.intensity,
        intensity_after=plan.requested if plan else None,
        intensity_mode=plan.mode if plan else None,
        intensity_before=plan.before if plan else None,
        intensity_requested=plan.requested if plan else None,
        intensity_limit=plan.limit if plan else None,
        intensity_clamped=plan.clamped if plan else False,
        watchdog_scheduled=watchdog,
    )
