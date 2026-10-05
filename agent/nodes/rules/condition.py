"""判定条件：**原子 → 组合 → 去抖**（T8 质量层）。

为什么拆这三层（协调者的原话）::

    "要考虑模型识别不是很准确，要加缓冲和综合判定，不能因为一下的大变形就判定"

* **原子**：一个可判定的基本事实（某关键点越线 / 落在区域 / 心率越阈 / 按钮按下）
* **组合**：多个原子 ``&&`` / ``||`` —— **综合判定**（比如"两只手都在线上方"）
* **去抖**：条件必须**连续成立 ≥ hold_ms** 才算数 —— **缓冲**。
  单帧的"大变形"或识别抖动**不会**立刻开火；中途失效则计时清零重新来
* **置信度过滤**：``visibility`` 低于阈值的关键点**不参与**判定 ——
  模型自己都说"这点看不清"，就不该拿它开火

参数语法（前端"画线 / 放框 / 放点"直接生成这个）::

    原子
      left_wrist:y>0.5            关键点越线（轴 x/y/z，坐标归一化 0~1）
      rect:nose,0.2,0.3,0.5,0.6   关键点落在矩形内（<点>,x0,y0,x1,y1）
      circle:left_wrist,0.5,0.5,0.08  关键点落在圆内（<点>,cx,cy,r）
      >120  /  <50                心率阈值
      A  /  B                     爪印按钮
    组合
      a && b                      都满足（默认）
      a || b                      任一满足
    维持
      <条件>@<秒>                 保持满 N 秒算通过；**中途被破坏**才罚

⚠️ ``&&`` 与 ``||`` **不许混用**（免得再引入优先级规则）—— 混用直接报错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "RuleError",
    "LANDMARKS",
    "LANDMARK_INDEX",
    "Sample",
    "Atom",
    "Condition",
    "HoldCondition",
    "Debouncer",
    "parse_atoms",
    "parse_condition",
    "guard_line",
    "hold_pose",
    "button",
    "heart_threshold",
    "DEFAULT_HOLD_MS",
    "DEFAULT_MIN_VISIBILITY",
]

# ──────────────────────────────────────────────────────────────
# MediaPipe Pose 的 33 个关键点（规则里用**名字**，不用裸索引）
# ──────────────────────────────────────────────────────────────
LANDMARKS: tuple[str, ...] = (
    "nose",
    "left_eye_inner", "left_eye", "left_eye_outer",
    "right_eye_inner", "right_eye", "right_eye_outer",
    "left_ear", "right_ear",
    "mouth_left", "mouth_right",
    "left_shoulder", "right_shoulder",
    "left_elbow", "right_elbow",
    "left_wrist", "right_wrist",
    "left_pinky", "right_pinky",
    "left_index", "right_index",
    "left_thumb", "right_thumb",
    "left_hip", "right_hip",
    "left_knee", "right_knee",
    "left_ankle", "right_ankle",
    "left_heel", "right_heel",
    "left_foot_index", "right_foot_index",
)

#: 常用别名（写规则/画线时少打字）
_ALIASES: dict[str, str] = {
    "lw": "left_wrist", "rw": "right_wrist",
    "le": "left_elbow", "re": "right_elbow",
    "ls": "left_shoulder", "rs": "right_shoulder",
    "lh": "left_hip", "rh": "right_hip",
    "lk": "left_knee", "rk": "right_knee",
    "la": "left_ankle", "ra": "right_ankle",
    "head": "nose",
}
LANDMARK_INDEX: dict[str, int] = {n: i for i, n in enumerate(LANDMARKS)}
LANDMARK_INDEX.update({a: LANDMARK_INDEX[t] for a, t in _ALIASES.items()})

#: 默认缓冲：条件要连续成立这么久才算数（250ms ≈ 7~8 帧 @30FPS）
DEFAULT_HOLD_MS = 250.0
#: 默认置信度门槛：visibility 低于它的关键点不参与判定
DEFAULT_MIN_VISIBILITY = 0.5


class RuleError(ValueError):
    """规则参数写法不对（装载时就要报错，别等运行时才发现）。"""


@dataclass
class Sample:
    """喂给规则的一条采样。"""

    kind: str  # "pose" | "heart" | "button"
    ts: float
    landmarks: list[list[float]] | None = None
    visibility: list[float] | None = None
    bpm: int | None = None
    channel: str | None = None

    def value(self, point: str, axis: str) -> float | None:
        """取某关键点的某轴坐标；**关键点不可信时返回 None**（调用方视为"不满足"）。"""
        if not self.landmarks:
            return None
        idx = LANDMARK_INDEX.get(point)
        if idx is None or idx >= len(self.landmarks):
            return None
        row = self.landmarks[idx]
        try:
            return float(row["xyz".index(axis)])
        except (IndexError, TypeError, ValueError):
            return None

    def visible(self, point: str, threshold: float) -> bool:
        """这个关键点的可见度够不够（没给 visibility 信息时**默认够**）。"""
        if threshold <= 0 or not self.visibility:
            return True
        idx = LANDMARK_INDEX.get(point)
        if idx is None or idx >= len(self.visibility):
            return True
        try:
            return float(self.visibility[idx]) >= threshold
        except (TypeError, ValueError):
            return True


# ══════════════════════════════════════════════════════════════
# 原子
# ══════════════════════════════════════════════════════════════
_ATTR = re.compile(r"^\s*(?P<point>[A-Za-z_]+)\s*:\s*(?P<axis>[xyzXYZ])\s*"
                   r"(?P<op>[<>])\s*(?P<value>[0-9]*\.?[0-9]+)\s*$")
_RECT = re.compile(r"^\s*rect\s*:\s*(?P<point>[A-Za-z_]+)\s*,\s*(?P<args>[0-9.,\s-]+)$", re.I)
_CIRCLE = re.compile(r"^\s*circle\s*:\s*(?P<point>[A-Za-z_]+)\s*,\s*(?P<args>[0-9.,\s-]+)$", re.I)
_HEART = re.compile(r"^\s*(?P<op>[<>])\s*(?P<value>[0-9]+)\s*$")
_HOLD = re.compile(r"^\s*(?P<inner>.+?)\s*@\s*(?P<sec>[0-9]*\.?[0-9]+)\s*$")


@dataclass(frozen=True)
class Atom:
    """一个原子判定。"""

    kind: str                                     # attr | rect | circle | heart | button
    point: str = ""
    axis: str = ""
    op: str = ""
    value: float = 0.0
    rect: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    circle: tuple[float, float, float] = (0.0, 0.0, 0.0)
    channel: str = ""

    # ── 判定 ────────────────────────────────────────────────
    def satisfied(self, sample: Sample, *, min_visibility: float = 0.0) -> bool:
        if self.kind == "heart":
            if sample.kind != "heart" or sample.bpm is None:
                return False
            return sample.bpm > self.value if self.op == ">" else sample.bpm < self.value
        if self.kind == "button":
            return sample.kind == "button" and (sample.channel or "").upper() == self.channel
        if sample.kind != "pose":
            return False
        if not sample.visible(self.point, min_visibility):
            return False                              # 模型都说看不清 → 不当证据
        if self.kind == "attr":
            v = sample.value(self.point, self.axis)
            if v is None:
                return False
            return v > self.value if self.op == ">" else v < self.value
        if self.kind in ("rect", "circle"):
            x = sample.value(self.point, "x")
            y = sample.value(self.point, "y")
            if x is None or y is None:
                return False
            if self.kind == "rect":
                x0, y0, x1, y1 = self.rect
                lo_x, hi_x = min(x0, x1), max(x0, x1)
                lo_y, hi_y = min(y0, y1), max(y0, y1)
                return lo_x <= x <= hi_x and lo_y <= y <= hi_y
            cx, cy, r = self.circle
            return (x - cx) ** 2 + (y - cy) ** 2 <= r * r
        return False

    def describe(self) -> str:
        if self.kind == "attr":
            return f"{self.point}:{self.axis}{self.op}{self.value:g}"
        if self.kind == "rect":
            return f"rect:{self.point}," + ",".join(f"{v:g}" for v in self.rect)
        if self.kind == "circle":
            return f"circle:{self.point}," + ",".join(f"{v:g}" for v in self.circle)
        if self.kind == "heart":
            return f"心率{self.op}{self.value:g}"
        return f"按钮{self.channel}"


def _point_of(name: str) -> str:
    """关键点名（含别名）→ 规范化名字；不认识就报错。"""
    point = (name or "").strip().lower()
    if point not in LANDMARK_INDEX:
        raise RuleError(f"未知关键点 {point!r}（可用 nose/left_wrist/left_hip… 共 33 个）")
    return point


def _parse_floats(text: str, count: int, what: str) -> tuple[float, ...]:
    parts = [p for p in re.split(r"[,\s]+", text.strip()) if p]
    if len(parts) != count:
        raise RuleError(f"{what} 需要 {count} 个数字，得到 {len(parts)} 个：{text!r}")
    try:
        return tuple(float(p) for p in parts)  # type: ignore[return-value]
    except ValueError as exc:
        raise RuleError(f"{what} 里有非数字：{text!r}") from exc


def parse_atom(expr: str, *, allow_button_heart: bool = False) -> Atom:
    """一段表达式 → :class:`Atom`（认不出来就抛 :class:`RuleError`）。"""
    text = (expr or "").strip()
    if not text:
        raise RuleError("空的判定条件")

    m = _ATTR.match(text)
    if m:
        return Atom(kind="attr", point=_point_of(m.group("point")),
                    axis=m.group("axis").lower(),
                    op=m.group("op"), value=float(m.group("value")))

    m = _RECT.match(text)
    if m:
        point = _point_of(m.group("point"))
        x0, y0, x1, y1 = _parse_floats(m.group("args"), 4, "矩形 rect:<点>,x0,y0,x1,y1")
        # 矩形得有点面积，否则是"线"不是"框"
        if abs(x1 - x0) < 1e-6 or abs(y1 - y0) < 1e-6:
            raise RuleError("矩形面积不能为 0（要一条线请用 a:y>v / a:x<v）")
        return Atom(kind="rect", point=point, rect=(x0, y0, x1, y1))

    m = _CIRCLE.match(text)
    if m:
        point = _point_of(m.group("point"))
        cx, cy, r = _parse_floats(m.group("args"), 3, "圆 circle:<点>,cx,cy,r")
        if r <= 0:
            raise RuleError("圆的半径必须 > 0")
        return Atom(kind="circle", point=point, circle=(cx, cy, r))

    if allow_button_heart:
        ch = text.upper()
        if ch in ("A", "B"):
            return Atom(kind="button", channel=ch)
        m = _HEART.match(text)
        if m:
            return Atom(kind="heart", op=m.group("op"), value=float(m.group("value")))

    raise RuleError(f"看不懂的判定条件 {expr!r}（例：left_wrist:y>0.5 / rect:0.2,0.3,0.5,0.6）")


def parse_atoms(expr: str, *, allow_button_heart: bool = False) -> tuple[list[Atom], str]:
    """``a && b`` / ``a || b`` → ``(atoms, mode)``；单条就是 ``mode="all"``。

    ``&&`` 与 ``||`` **混用会报错** —— 免得引入优先级规则（前端画线也不会生成混用）。
    """
    text = (expr or "").strip()
    if "&&" in text and "||" in text:
        raise RuleError("同一条规则里不许混用 && 和 ||（拆成两条规则更清楚）")
    if "||" in text:
        parts, mode = text.split("||"), "any"
    elif "&&" in text:
        parts, mode = text.split("&&"), "all"
    else:
        parts, mode = [text], "all"
    atoms = [parse_atom(p, allow_button_heart=allow_button_heart) for p in parts if p.strip()]
    if not atoms:
        raise RuleError(f"判定条件为空：{expr!r}")
    return atoms, mode


# ══════════════════════════════════════════════════════════════
# 组合条件
# ══════════════════════════════════════════════════════════════
@dataclass
class Condition:
    """若干原子的**组合**（综合判定）。

    :param mode: ``"all"``（都满足，默认）| ``"any"``（任一满足）
    """

    atoms: list[Atom] = field(default_factory=list)
    mode: str = "all"

    def satisfied(self, sample: Sample, *, min_visibility: float = 0.0) -> bool:
        if not self.atoms:
            return False
        results = (a.satisfied(sample, min_visibility=min_visibility) for a in self.atoms)
        return all(results) if self.mode == "all" else any(results)

    def describe(self) -> str:
        joiner = " 且 " if self.mode == "all" else " 或 "
        return joiner.join(a.describe() for a in self.atoms)


@dataclass
class HoldCondition:
    """维持姿势：``<条件>@<秒>``。

    条件成立开始计时；**保持满 N 秒算通过**（不惩罚并重新开始），
    **中途被破坏且之前确实在保持** → 触发惩罚。

    历史：T8 只有 ``GuardLine`` 参与维持；现在组合条件（含区域）都能维持。
    """

    condition: Condition
    seconds: float
    _since: float | None = None

    def satisfied(self, sample: Sample, *, min_visibility: float = 0.0) -> bool:
        """返回"**本次是否要罚**"（语义与 :class:`Condition` 不同，见类文档）。"""
        if sample.kind != "pose":
            return False
        if self.condition.satisfied(sample, min_visibility=min_visibility):
            if self._since is None:
                self._since = sample.ts
            elif sample.ts - self._since >= self.seconds:
                self._since = None            # 保持满 → 通过，重新开始
            return False
        if self._since is not None:           # 破坏
            self._since = None
            return True
        return False

    def reset(self) -> None:
        self._since = None

    def describe(self) -> str:
        return f"{self.condition.describe()} 保持 {self.seconds:g}s"


# ══════════════════════════════════════════════════════════════
# 去抖（缓冲）
# ══════════════════════════════════════════════════════════════
@dataclass
class Debouncer:
    """条件必须**连续成立 ≥ hold_ms** 才放行一次（缓冲/去抖）。

    语义（边沿 + 时长）::

        成立 ──计时──> 满 hold_ms ──> 放行一次（保持"已放行"，直到条件失效）
        中途失效 ──> 计时清零（下次重新攒时间）

    所以"一下的大变形"（只成立一两帧）**不会**触发；持续越线才会。
    ``hold_ms=0`` 等价于过去的"上升沿立即触发"。
    """

    hold_ms: float = 0.0
    _since: float | None = None
    _fired: bool = False

    def feed(self, ok: bool, ts: float) -> bool:
        """``ts`` 是**秒**。返回 True 表示"本次应当触发"。"""
        if not ok:
            self._since = None
            self._fired = False
            return False
        if self._since is None:
            self._since = ts
            if self.hold_ms <= 0:          # 不缓冲 → 直接算成立
                self._fired = True
                return True
            return False
        if self._fired:
            return False                   # 已经放行过，别重复触发
        if (ts - self._since) * 1000.0 >= self.hold_ms:
            self._fired = True
            return True
        return False

    def reset(self) -> None:
        self._since = None
        self._fired = False


# ══════════════════════════════════════════════════════════════
# 按 cond 名构造
# ══════════════════════════════════════════════════════════════
_COND_KINDS: tuple[str, ...] = ("警戒线", "维持姿势", "按钮", "心率阈值")


def parse_condition(cond: str, param: str) -> Condition | HoldCondition:
    """``(cond, param)`` → 条件对象（非法写法抛 :class:`RuleError`）。"""
    name = (cond or "").strip()
    if name == "维持姿势":
        m = _HOLD.match(param or "")
        if not m:
            raise RuleError(f"维持姿势参数格式应为 <条件>@<秒>，得到 {param!r}")
        seconds = float(m.group("sec"))
        if seconds <= 0:
            raise RuleError("维持姿势的秒数必须 > 0")
        atoms, mode = parse_atoms(m.group("inner"), allow_button_heart=True)
        return HoldCondition(Condition(atoms, mode), seconds)

    if name == "警戒线":
        atoms, mode = parse_atoms(param)
        for atom in atoms:
            if atom.kind in ("heart", "button"):
                raise RuleError("警戒线只能用姿态/区域条件；心率与按钮请用对应类型")
        return Condition(atoms, mode)

    if name == "按钮":
        atom = parse_atom(param, allow_button_heart=True)
        if atom.kind != "button":
            raise RuleError(f"按钮类型的参数应为 A 或 B，得到 {param!r}")
        return Condition([atom], "all")

    if name == "心率阈值":
        atom = parse_atom(param, allow_button_heart=True)
        if atom.kind != "heart":
            raise RuleError(f"心率阈值的参数应为 >120 或 <50，得到 {param!r}")
        return Condition([atom], "all")

    raise RuleError(f"未知条件类型 {cond!r}（可用：{'/'.join(_COND_KINDS)}）")


# ──────────────────────────────────────────────────────────────
# 便捷构造（等价于 parse_condition 的对应分支，便于测试与简单调用）
# ──────────────────────────────────────────────────────────────
def guard_line(expr: str) -> Condition:
    """单条/组合警戒线表达式 → :class:`Condition`。"""
    atoms, mode = parse_atoms(expr)
    for atom in atoms:
        if atom.kind in ("heart", "button"):
            raise RuleError("警戒线只能用姿态/区域条件；心率与按钮请用对应类型")
    return Condition(atoms, mode)


def hold_pose(expr: str) -> HoldCondition:
    out = parse_condition("维持姿势", expr)
    assert isinstance(out, HoldCondition)
    return out


def button(expr: str) -> Condition:
    out = parse_condition("按钮", expr)
    assert isinstance(out, Condition)
    return out


def heart_threshold(expr: str) -> Condition:
    out = parse_condition("心率阈值", expr)
    assert isinstance(out, Condition)
    return out
