"""``rules`` 节点（T8）—— 判定引擎：**控制端判定，被控端纯执行**。

* **订阅**：``feature/pose``、``feature/heart``、``event/pawprint``、``command/rules``
* **发布**：``command/fire``（触发惩罚 = 下发指定波形，**不是堆强度**）

规则模型与前端 ``web/js/rules.js`` 的表单字段一一对应
（``id/name/cond/param/wave/channel/dur/cd/enabled``，外加两个质量字段
``debounce_ms`` / ``min_visibility``）；条件的解析与判定在 :mod:`.condition`。

**为什么要有 `debounce_ms` / `min_visibility`**（协调者原话）::

    "要考虑模型识别不是很准确，要加缓冲和综合判定，不能因为一下的大变形就判定"

* **缓冲**：条件要**连续成立 ≥ ``debounce_ms``**（默认 250ms）才算数 —— 单帧抖动不开火；
* **综合判定**：``param`` 支持 ``a && b``（都满足）/ ``a || b``（任一满足），
  还能用 ``rect:…`` / ``circle:…`` 判定"某个关键点落在区域里"（前端画框用）；
* **置信度过滤**：``visibility`` 低于 ``min_visibility``（默认 0.5）的关键点**不参与**判定。

规则来源：节点启动参数 / ``data/rules.json`` / **面板下发**（``command/rules``，整体替换）。
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.core import Node, topics
from agent.core.messages import CommandFire

from .condition import (
    DEFAULT_HOLD_MS,
    DEFAULT_MIN_VISIBILITY,
    LANDMARK_INDEX,
    LANDMARKS,
    Atom,
    Condition,
    Debouncer,
    HoldCondition,
    RuleError,
    Sample,
    parse_condition,
)

__all__ = [
    "RulesNode",
    "Rule",
    "RuleRuntime",
    "RuleError",
    "Sample",
    "Condition",
    "HoldCondition",
    "Atom",
    "Debouncer",
    "parse_condition",
    "LANDMARKS",
    "LANDMARK_INDEX",
    "DEFAULT_HOLD_MS",
    "DEFAULT_MIN_VISIBILITY",
]


# ══════════════════════════════════════════════════════════════
# 规则 & 运行时
# ══════════════════════════════════════════════════════════════
@dataclass
class Rule:
    """一条规则（字段与前端 ``rules.js`` 一致）。

    :param debounce_ms: **缓冲**：条件要连续成立这么久才算数（0 = 边沿立即触发）
    :param min_visibility: **置信度门槛**：关键点可见度低于它就不参与判定
    """

    id: str
    name: str
    cond: str
    param: str = ""
    wave: str = ""
    channel: str = "A"
    dur: int = 2000
    cd: float = 0.0
    enabled: bool = True
    debounce_ms: float = DEFAULT_HOLD_MS
    min_visibility: float = DEFAULT_MIN_VISIBILITY

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Rule":
        if not isinstance(data, dict):
            raise RuleError(f"规则必须是对象，得到 {type(data).__name__}")
        cond = str(data.get("cond") or "").strip()
        if not cond:
            raise RuleError("规则缺少 cond")
        channel = str(data.get("channel") or "A").strip().upper()
        if channel not in ("A", "B"):
            raise RuleError(f"通道只能是 A/B，得到 {channel!r}")
        raw_db = data.get("debounce_ms")
        raw_vis = data.get("min_visibility")
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or "").strip() or cond,
            cond=cond,
            param=str(data.get("param") or ""),
            wave=str(data.get("wave") or ""),
            channel=channel,
            dur=int(data.get("dur") or 2000),
            cd=float(data.get("cd") or 0),
            enabled=bool(data.get("enabled", True)),
            debounce_ms=DEFAULT_HOLD_MS if raw_db is None else float(raw_db),
            min_visibility=DEFAULT_MIN_VISIBILITY if raw_vis is None else float(raw_vis),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "cond": self.cond, "param": self.param,
            "wave": self.wave, "channel": self.channel, "dur": self.dur,
            "cd": self.cd, "enabled": self.enabled,
            "debounce_ms": self.debounce_ms, "min_visibility": self.min_visibility,
        }

    def compile(self) -> Condition | HoldCondition:
        """``(cond, param)`` → 可判定的条件对象（写错在这里就报错）。"""
        return parse_condition(self.cond, self.param)

    def to_command(self) -> CommandFire:
        """变成一条 ``command/fire``（惩罚 = 开火一个波形）。"""
        return CommandFire(
            waveform=self.wave or "PULSE",
            channel=self.channel,
            duration_ms=int(self.dur),
            priority=1,
            overlay=True,
            intensity_delta=None,
            intensity=None,
        )


@dataclass
class RuleRuntime:
    """规则 + 它的条件实例 + **去抖** + 冷却。"""

    rule: Rule
    condition: Condition | HoldCondition
    last_fire: float = -1e18
    fires: int = 0
    debounce: Debouncer = field(default_factory=Debouncer)

    def __post_init__(self) -> None:
        self.debounce = Debouncer(self.rule.debounce_ms)

    @property
    def cd(self) -> float:
        return max(0.0, float(self.rule.cd))

    def feed(self, sample: Sample) -> bool:
        """喂一条采样；返回 True 表示**本次要开火**。"""
        if not self.rule.enabled:
            return False
        cond = self.condition
        if isinstance(cond, HoldCondition):
            # 维持姿势自带"保持满算过、破坏才罚"的语义，本身就有秒级时间尺度，
            # 不再叠加 ms 级去抖（否则语义含糊）
            hit = cond.satisfied(sample, min_visibility=self.rule.min_visibility)
        else:
            ok = cond.satisfied(sample, min_visibility=self.rule.min_visibility)
            hit = self.debounce.feed(ok, sample.ts)
        if not hit:
            return False
        if sample.ts - self.last_fire < self.cd:      # 冷却中
            return False
        self.last_fire = sample.ts
        self.fires += 1
        return True


# ══════════════════════════════════════════════════════════════
# 节点
# ══════════════════════════════════════════════════════════════
class RulesNode(Node):
    """判定引擎节点。

    :param rules: 初始规则（``dict`` / :class:`Rule` 均可）
    :param rules_file: 从 JSON 文件读规则（``{"rules": [...]}`` 或裸数组）
    :param clock: 时间源（测试可注入假时钟）
    """

    name = "rules"

    def __init__(
        self,
        config: Any | None = None,
        *,
        rules: list[Any] | None = None,
        rules_file: str | Path | None = None,
        clock: Any = time.time,
    ) -> None:
        super().__init__(config)
        self._initial = list(rules or [])
        self._rules_file = Path(rules_file) if rules_file is not None else None
        self._clock = clock

        self.runtimes: list[RuleRuntime] = []
        self.errors: list[str] = []          # 装载失败的原因（不打断其它规则）
        self.fired: list[dict[str, Any]] = []
        self._pub: Any = None
        self._subs: list[Any] = []

    # ──────────────────────────────────────────────────────
    # 生命周期
    # ──────────────────────────────────────────────────────
    async def setup(self) -> None:
        self._pub = self.publish(topics.COMMAND_FIRE)

        load: list[Any] = list(self._initial)
        if self._rules_file is not None and self._rules_file.exists():
            try:
                raw = json.loads(self._rules_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    raw = raw.get("rules", [])
                load.extend(raw or [])
                self.log.info("从 %s 读入 %d 条规则", self._rules_file, len(raw or []))
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"读规则文件失败：{exc}")
                self.log.error("读规则文件失败：%s", exc)
        self.set_rules(load)

        self._subs = [
            self.subscribe(topics.FEATURE_POSE, self.on_pose),
            self.subscribe(topics.FEATURE_HEART, self.on_heart),
            self.subscribe(topics.EVENT_PAWPRINT, self.on_pawprint),
            self.subscribe(topics.COMMAND_RULES, self.on_rules),
        ]
        self.log.info(
            "rules 就绪：装载 %d 条（失败 %d 条），发布 %s",
            len(self.runtimes), len(self.errors), topics.COMMAND_FIRE,
        )

    async def teardown(self) -> None:
        self._subs = []

    # ──────────────────────────────────────────────────────
    # 规则装载（热更新）
    # ──────────────────────────────────────────────────────
    def set_rules(self, rules: list[Any]) -> int:
        """整体替换规则集。返回成功装载的条数；**坏规则只记录不中断**。"""
        runtimes: list[RuleRuntime] = []
        errors: list[str] = []
        for i, raw in enumerate(rules or []):
            try:
                rule = raw if isinstance(raw, Rule) else Rule.from_dict(raw)
                runtimes.append(RuleRuntime(rule=rule, condition=rule.compile()))
            except RuleError as exc:
                errors.append(f"第 {i} 条规则：{exc}")
        self.runtimes = runtimes
        self.errors = errors
        for e in errors:
            self.log.warning("规则装载失败 —— %s", e)
        return len(runtimes)

    def add_rule(self, raw: Any) -> set:
        try:
            rule = raw if isinstance(raw, Rule) else Rule.from_dict(raw)
            self.runtimes.append(RuleRuntime(rule=rule, condition=rule.compile()))
            return set()
        except RuleError as exc:
            self.errors.append(str(exc))
            return {str(exc)}

    # ──────────────────────────────────────────────────────
    # 订阅 handler
    # ──────────────────────────────────────────────────────
    async def on_pose(self, msg: Any) -> None:
        data = _as_mapping(msg)
        if not data or not data.get("present", True):
            return
        self._feed_samples(
            Sample(
                kind="pose",
                ts=self._now(data),
                landmarks=data.get("landmarks"),
                visibility=data.get("visibility"),
            )
        )

    async def on_heart(self, msg: Any) -> None:
        data = _as_mapping(msg)
        if not data or data.get("bpm") is None:
            return
        self._feed_samples(Sample(kind="heart", ts=self._now(data), bpm=int(data["bpm"])))

    async def on_pawprint(self, msg: Any) -> None:
        data = _as_mapping(msg)
        if not data:
            return
        # 只有"按下/触发"类事件才算（具体字段等 T7 爪印落地后对齐，两种写法都认）
        action = str(data.get("action") or data.get("event") or "press").lower()
        if action in ("release", "up", "idle"):
            return
        self._feed_samples(
            Sample(
                kind="button",
                ts=self._now(data),
                channel=str(data.get("channel") or data.get("button") or "") or None,
            )
        )

    async def on_rules(self, msg: Any) -> None:
        """``command/rules`` —— 面板下发的规则集（**整体替换**语义）。

        payload 两种写法都认：裸数组 ``[…]``，或 ``{"rules":[…]}`；
        每条规则的字段与前端 ``web/js/rules.js`` 的表单一 一对应
        （``id / name / cond / param / wave / channel / dur / cd / enabled`` +
        ``debounce_ms / min_visibility``）。
        """
        if isinstance(msg, list):
            rules: Any = msg
        else:
            data = _as_mapping(msg)
            rules = data.get("rules") if isinstance(data, dict) else None
        if not isinstance(rules, list):
            self.log.warning("规则下发的 payload 应是数组或 {rules:[…]}，收到：%r", msg)
            return
        loaded = self.set_rules(rules)
        self.log.info("规则已更新：装载 %d 条（失败 %d 条）", loaded, len(self.errors))

    # ──────────────────────────────────────────────────────
    # 内部
    # ──────────────────────────────────────────────────────
    def _now(self, data: dict[str, Any]) -> float:
        """采样时间戳：优先消息里的 ``ts``（毫秒），否则用时钟。"""
        ts = data.get("ts")
        if isinstance(ts, (int, float)) and ts > 0:
            return float(ts) / 1000.0
        return float(self._clock())

    def _feed_samples(self, sample: Sample) -> None:
        for rt in list(self.runtimes):
            try:
                if rt.feed(sample):
                    self._fire(rt.rule)
            except Exception:  # noqa: BLE001 - 一条规则出错不能影响其它规则
                self.log.exception("规则 %s 判定出错", rt.rule.name)

    def _fire(self, rule: Rule) -> None:
        cmd = rule.to_command()
        record = {"ts": self._clock() * 1000.0, "rule": rule.name, "cond": rule.cond,
                  "param": rule.param, "waveform": cmd.waveform, "channel": cmd.channel,
                  "duration_ms": cmd.duration_ms}
        self.fired.append(record)
        if len(self.fired) > 512:
            del self.fired[: len(self.fired) - 512]
        if self._pub is not None:
            self._pub.put(cmd)
        self.log.info("🔥 规则触发「%s」（%s）→ 开火 %s %s %dms",
                      rule.name, rule.cond, cmd.waveform, cmd.channel, cmd.duration_ms)


def _as_mapping(msg: Any) -> dict[str, Any] | None:
    """订阅侧拿到的东西 → dict（``Message`` / ``dict`` / JSON ``str``/``bytes`` 都认）。"""
    if msg is None:
        return None
    if isinstance(msg, dict):
        return msg
    to_dict = getattr(msg, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    if isinstance(msg, (bytes, bytearray, memoryview)):
        with contextlib.suppress(Exception):
            v = json.loads(bytes(msg).decode("utf-8"))
            return v if isinstance(v, dict) else None
        return None
    if isinstance(msg, str):
        with contextlib.suppress(Exception):
            v = json.loads(msg)
            return v if isinstance(v, dict) else None
    return None
