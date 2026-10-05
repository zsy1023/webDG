"""波形导入：**V2 / V3 两种帧格式**的识别、解析、校验、时长适配与转换。

依据（只读，不修改）：

- `docs/tasks/T4-郊狼V4控制端.md` §4.6（帧格式规格、识别与校验规则、接口）
- `reference/dglab-bluetooth-protocol/coyote/v2/README.md`（V2 的 X/Y/Z 位域与语义）
- `reference/dglab-websocket-simple/socket/DG_WAVES_V2_V3_simple.js`（官方 16 条测试向量）

两种帧格式
----------

**V3 帧**：16 个 hex 字符 = 8 字节 = 4 组 ``(频率, 强度)``，每组 25ms，合计 **100ms/帧**。
频率 10~240（``< 10`` 视作静音，见下），强度 0~100。

**V2 帧**：6 个 hex 字符 = 3 字节 = **小端 24 位位打包**，同样 **100ms/帧**：

.. code-block:: text

    bit  4..0  = X  (5 bit)   连续脉冲个数（0~31）
    bit 14..5  = Y  (10 bit)  脉冲间隔 ms（0~1023）
    bit 19..15 = Z  (5 bit)   脉冲宽度（实际宽度 = Z*5us）
    bit 23..20 = 保留（实测官方向量恒为 0）

⚠️ **V2 与 V3 之间不是无损互转**（下节有实测依据），因此：

- :func:`to_v2` / :func:`to_v3` 是**有损的"尽力而为"转换**，只保证可确定的部分
  （强度维度可精确往返；频率可往返 ``f == X + Y``；反之拆不回去）；
- 导入 V2 帧时**保持原样**，下发时通过 ``options.version = 2`` 告诉 SDK 这是 V2 帧；
  不要"顺手转成 V3"，那只会丢掉精度。

关于官方 16 条测试向量（实测 693 帧全量分析结论，见 ``tests/test_waveform_io.py``）
----------------------------------------------------------------------------

1. 每条向量的 ``expectedV2`` / ``expectedV3`` **不是互转对**，而是同一条 raw
   （``Dungeonlab+pulse:`` 格式，任务书列为可选）的**两种独立编码**；
2. ``Z == V3强度 // 5`` 在 693/693 帧上成立 —— 强度维度是精确可逆的；
3. 但 V2→V3 的频率只在 619/693 帧上等于 ``X + Y``（74 帧例外，如
   ``"46130A"``：X=6, Y=154 却对应 V3 频率 112）；
4. 同一个 V3 频率可对应多个 X（f=112 → X∈{3,6}），所以 **X 无法从 V3 反推**；
5. V2 的 ``"000000"``（X=0, Y=0）是**静音帧**，对应 V3 的 频率 10 / 强度 0（21 帧）；
6. V3 每帧的 4 组恒相同（693/693）—— 这也是 V2→V3 展开时"4 组相同"的依据。

单位与常量全部集中在 :data:`FRAME_MS`，不要散落魔法数字。
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger("coyotelink.waveform_io")

__all__ = [
    "FRAME_MS",
    "V2_FRAME_HEX",
    "V3_FRAME_HEX",
    "V2_FRAME_BYTES",
    "V3_FRAME_BYTES",
    "FREQ_MIN",
    "FREQ_MAX",
    "SILENT_FREQ",
    "STRENGTH_MIN",
    "STRENGTH_MAX",
    "V2_X_MAX",
    "V2_Y_MAX",
    "V2_Z_MAX",
    "STRENGTH_PER_Z",
    "WaveformError",
    "Waveform",
    "V2Params",
    "decode_v2_frame",
    "encode_v2_frame",
    "decode_v3_frame",
    "encode_v3_frame",
    "frame_hex_len",
    "detect_version",
    "validate",
    "normalize_frames",
    "import_json",
    "import_text",
    "import_file",
    "frame_count_for_duration",
    "frames_for_duration",
    "frames_span_ms",
    "to_v3",
    "to_v2",
    "official_waveforms",
    "load_all",
    "find",
]

# ──────────────────────────────────────────────────────────────
# 规格常量（全部来自任务书 §4.6.1 与 reference/ 的官方协议文档）
# ──────────────────────────────────────────────────────────────
#: 一帧覆盖的时长（ms）。V2 / V3 都是 100ms。
FRAME_MS = 100

#: V3 帧 = 16 个 hex 字符 = 8 字节（4 组「频率,强度」，每组 25ms）
V3_FRAME_HEX = 16
V3_FRAME_BYTES = 8

#: V2 帧 = 6 个 hex 字符 = 3 字节（小端 24 位位打包）
V2_FRAME_HEX = 6
V2_FRAME_BYTES = 3

#: V3 频率字段范围（任务书 §4.6.1）；低于 FREQ_MIN 按静音处理
FREQ_MIN = 10
FREQ_MAX = 240

#: 静音频率：V2 的 "000000" 在 V3 里就是 (频率 10, 强度 0)
SILENT_FREQ = FREQ_MIN

STRENGTH_MIN = 0
STRENGTH_MAX = 100

V2_X_MAX = 0x1F  # 5 bit
V2_Y_MAX = 0x3FF  # 10 bit
V2_Z_MAX = 0x1F  # 5 bit

#: 强度 —— 脉冲宽度 Z（Z*5us）—— 的换算系数：Z = 强度 // 5
STRENGTH_PER_Z = 5

_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")


class WaveformError(ValueError):
    """波形数据不合法（帧长混用、非法 hex、版本声明与帧不符……）。

    错误信息**必须带帧号**（1-based，人看的序号）—— 这是任务书 D14 的硬要求。
    """


# ──────────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────────
@dataclass
class Waveform:
    """一条波形。

    ``version`` 只能是 2 或 3；``source`` 是 ``"official" | "imported" | "raw"``。
    ``aliases`` 是查表用的别名（官方波形的中/英文 label），不属于任务书要求的最小字段，
    但让 ``fire("气泡")`` 也能工作。
    """

    name: str
    frames: list[str]
    version: int
    source: str = "imported"
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise WaveformError("Waveform.name 必须是非空字符串")
        if not isinstance(self.frames, list) or not self.frames:
            raise WaveformError(f"波形 {self.name!r} 没有任何帧")
        self.frames = normalize_frames(self.frames)
        detected = detect_version(self.frames)
        if self.version not in (2, 3):
            raise WaveformError(f"波形 {self.name!r} 的 version 只能是 2 或 3，得到 {self.version!r}")
        if detected != self.version:
            raise WaveformError(
                f"波形 {self.name!r} 声明 version={self.version}，"
                f"但帧格式是 V{detected}（每帧 {frame_hex_len(self.frames[0])} 个 hex 字符）"
            )
        self.aliases = tuple(self.aliases)

    # —— 便利属性 ——
    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def natural_duration_ms(self) -> int:
        """这条波形自然播放一遍的时长（= 帧数 × 100ms）。"""
        return len(self.frames) * FRAME_MS

    def __len__(self) -> int:  # 让 `len(w)` 也能用
        return len(self.frames)

    def __repr__(self) -> str:  # pragma: no cover - 调试友好
        return (
            f"<Waveform {self.name!r} v{self.version} frames={len(self.frames)} "
            f"source={self.source!r}>"
        )


@dataclass(frozen=True)
class V2Params:
    """V2 帧解出的三个参数（外加保留位，仅供校验）。"""

    x: int
    y: int
    z: int
    reserved: int = 0

    @property
    def frequency(self) -> int:
        """V2 的频率特征值 ``Frequency = X + Y``（官方 v2 文档 §Frequency 值）。"""
        return self.x + self.y

    @property
    def is_silent(self) -> bool:
        return self.x == 0 and self.y == 0


# ──────────────────────────────────────────────────────────────
# 帧的基本校验
# ──────────────────────────────────────────────────────────────
def frame_hex_len(frame: Any) -> int:
    """返回帧的 hex 字符数；不合法就抛错（**不含帧号**，由调用方补）。

    校验顺序：类型 → 非空 → 是否 hex → 是否偶数长度。
    """
    if not isinstance(frame, str):
        raise WaveformError(f"帧必须是字符串，得到 {type(frame).__name__}")
    text = frame.strip()
    if not text:
        raise WaveformError("帧是空字符串")
    if not _HEX_RE.match(text):
        bad = next((ch for ch in text if ch not in "0123456789abcdefABCDEF"), "?")
        raise WaveformError(f"帧 {text!r} 含非法 hex 字符 {bad!r}")
    if len(text) % 2:
        raise WaveformError(f"帧 {text!r} 的 hex 长度是奇数（{len(text)}），不是整数字节")
    return len(text)


def _frame_hex_len_at(frame: Any, index: int) -> int:
    """:func:`frame_hex_len` 的带帧号版本（帧号 1-based，给 D14 用）。"""
    try:
        return frame_hex_len(frame)
    except WaveformError as exc:
        raise WaveformError(f"第 {index + 1} 帧：{exc}") from exc


def normalize_frames(frames: Iterable[Any]) -> list[str]:
    """逐帧去空白并**统一大写**（hex 大小写不敏感，内部统一成大写便于比较）。"""
    out: list[str] = []
    for index, frame in enumerate(frames):
        _frame_hex_len_at(frame, index)
        out.append(str(frame).strip().upper())
    return out


def detect_version(frames: Sequence[str]) -> int:
    """按帧长度识别波形版本：6 hex → V2，16 hex → V3。

    - 空列表 → 报错；
    - 同一条波形内**帧长必须一致**，混用直接报错（任务书 §4.6.3 规则 2：**不要猜**）；
    - 长度既不是 6 也不是 16 → 报错，并说明是哪一帧、长度是多少。
    """
    if not frames:
        raise WaveformError("波形没有任何帧，无法识别版本")
    first = _frame_hex_len_at(frames[0], 0)
    for index in range(1, len(frames)):
        length = _frame_hex_len_at(frames[index], index)
        if length != first:
            raise WaveformError(
                f"第 {index + 1} 帧长 {length} 个 hex 字符，与第 1 帧的 {first} 不一致；"
                "同一条波形内 V2(6)/V3(16) 帧不能混用"
            )
    if first == V2_FRAME_HEX:
        return 2
    if first == V3_FRAME_HEX:
        return 3
    raise WaveformError(
        f"第 1 帧长 {first} 个 hex 字符，既不是 V2 的 {V2_FRAME_HEX} 也不是 V3 的 {V3_FRAME_HEX}"
    )


def validate(frames: Sequence[str]) -> None:
    """整条波形的校验：非法 hex / 混合长度 → 抛 :class:`WaveformError`（带帧号）。

    相当于 :func:`detect_version` 的"只要副作用"版本，语义上更直白。
    """
    detect_version(list(frames))


# ──────────────────────────────────────────────────────────────
# 帧编解码
# ──────────────────────────────────────────────────────────────
def decode_v2_frame(frame: str) -> V2Params:
    """V2 帧 → :class:`V2Params`（小端 24 位按位拆 X / Y / Z / 保留位）。"""
    if frame_hex_len(frame) != V2_FRAME_HEX:
        raise WaveformError(f"不是 V2 帧（应为 {V2_FRAME_HEX} 个 hex 字符）：{frame!r}")
    value = int.from_bytes(bytes.fromhex(frame), "little")
    return V2Params(
        x=value & V2_X_MAX,
        y=(value >> 5) & V2_Y_MAX,
        z=(value >> 15) & V2_Z_MAX,
        reserved=(value >> 20) & 0xF,
    )


def encode_v2_frame(x: int, y: int, z: int) -> str:
    """X / Y / Z → V2 帧（超范围报错，不静默截断）。"""
    for value, limit, tag in ((x, V2_X_MAX, "X"), (y, V2_Y_MAX, "Y"), (z, V2_Z_MAX, "Z")):
        if not isinstance(value, int) or isinstance(value, bool):
            raise WaveformError(f"V2 的 {tag} 必须是整数，得到 {value!r}")
        if not 0 <= value <= limit:
            raise WaveformError(f"V2 的 {tag} 超出范围 0~{limit}：{value}")
    packed = (x & V2_X_MAX) | ((y & V2_Y_MAX) << 5) | ((z & V2_Z_MAX) << 15)
    return packed.to_bytes(V2_FRAME_BYTES, "little").hex().upper()


def decode_v3_frame(frame: str) -> tuple[tuple[int, int], ...]:
    """V3 帧 → 4 组 ``(频率, 强度)``。"""
    if frame_hex_len(frame) != V3_FRAME_HEX:
        raise WaveformError(f"不是 V3 帧（应为 {V3_FRAME_HEX} 个 hex 字符）：{frame!r}")
    raw = bytes.fromhex(frame)
    return tuple((raw[i], raw[4 + i]) for i in range(4))  # type: ignore[return-value]


def encode_v3_frame(groups: Sequence[Sequence[int]]) -> str:
    """4 组 ``(频率, 强度)`` → V3 帧（组数必须是 4，值超范围报错）。

    帧布局与官方一致：**先 4 个频率字节，再 4 个强度字节**（任务书 §4.6.1）。
    """
    if len(groups) != 4:
        raise WaveformError(f"V3 帧需要 4 组 (频率,强度)，得到 {len(groups)} 组")
    freqs = bytearray()
    strengths = bytearray()
    for index, group in enumerate(groups):
        if len(group) != 2:
            raise WaveformError(f"第 {index + 1} 组应为 (频率,强度)，得到 {group!r}")
        freq, strength = int(group[0]), int(group[1])
        for value, limit, tag in ((freq, 255, "频率"), (strength, 255, "强度")):
            if not 0 <= value <= limit:
                raise WaveformError(f"第 {index + 1} 组的{tag}超出 0~{limit}：{value}")
        freqs.append(freq)
        strengths.append(strength)
    return bytes(freqs + strengths).hex().upper()


# ──────────────────────────────────────────────────────────────
# 时长 ↔ 帧数
# ──────────────────────────────────────────────────────────────
def frame_count_for_duration(duration_ms: int) -> int:
    """``duration_ms`` 需要多少帧（每帧 100ms，四舍五入，至少 1 帧）。"""
    if not isinstance(duration_ms, int) or isinstance(duration_ms, bool):
        raise WaveformError(f"duration_ms 必须是整数，得到 {duration_ms!r}")
    if duration_ms <= 0:
        raise WaveformError(f"duration_ms 必须为正数，得到 {duration_ms}")
    return max(1, math.floor(duration_ms / FRAME_MS + 0.5))


def frames_for_duration(waveform: Waveform, duration_ms: int) -> list[str]:
    """把波形适配到指定时长：帧数不够就**循环补齐**，多了就**截断**。

    返回的帧数 = :func:`frame_count_for_duration`，因此
    ``len(结果) * FRAME_MS`` 与 ``duration_ms`` 的误差不超过一帧（±10% @ 1s）。

    这是 SDK **不做**的适配（任务书 §6 第 3 条），必须自己写。
    """
    needed = frame_count_for_duration(duration_ms)
    frames = waveform.frames
    if needed == len(frames):
        return list(frames)
    if needed < len(frames):
        return list(frames[:needed])
    out: list[str] = []
    while len(out) < needed:
        out.extend(frames[: needed - len(out)])
    return out


def frames_span_ms(frames: Sequence[str]) -> int:
    """一组帧覆盖的时长（ms）。"""
    return len(frames) * FRAME_MS


# ──────────────────────────────────────────────────────────────
# 导入（JSON / 纯文本）
# ──────────────────────────────────────────────────────────────
def _build(name: str, frames: Sequence[str], version: int | None, source: str) -> Waveform:
    clean = normalize_frames(frames)
    if version is None:
        version = detect_version(clean)
    return Waveform(name=name, frames=clean, version=version, source=source)


def import_json(data: Any, name: str | None = None, *, source: str = "imported") -> Waveform:
    """从 JSON（dict / JSON 文本 / 文件对象）导入一条波形。

    结构（任务书 §4.6.4 A，``version`` 可省 —— 省略则自动识别）::

        {"name": "我的波形", "version": 3, "frames": ["0A0A0A0A00000000", "..."]}
    """
    if isinstance(data, (str, bytes, bytearray)):
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise WaveformError(f"JSON 解析失败：{exc}") from exc
    else:
        payload = data
    if not isinstance(payload, dict):
        raise WaveformError(f"波形 JSON 顶层必须是对象，得到 {type(payload).__name__}")

    unknown = sorted(set(payload) - {"name", "version", "frames"})
    if unknown:
        logger.debug("波形 JSON 忽略未知字段：%s", ", ".join(unknown))

    frames = payload.get("frames")
    if not isinstance(frames, list):
        raise WaveformError("波形 JSON 缺少 'frames' 数组")
    # JSON 里显式写的 name 优先；没写才用调用方给的默认名（一般是文件名）
    wave_name = payload.get("name") or name
    if not isinstance(wave_name, str) or not wave_name.strip():
        raise WaveformError("波形 JSON 缺少 'name'（也没给默认名字）")
    version = payload.get("version")
    if version is not None and (not isinstance(version, int) or isinstance(version, bool)):
        raise WaveformError(f"波形 JSON 的 'version' 必须是整数 2 或 3，得到 {version!r}")
    return _build(wave_name.strip(), frames, version, source)


def import_text(text: str, name: str) -> Waveform:
    """从纯文本导入（任务书 §4.6.4 B）：每行一帧，支持 ``#`` 注释与空行。

    行内也可以用空白或逗号分隔多个帧（方便从日志/表格里粘）。
    """
    if not isinstance(text, str):
        raise WaveformError(f"text 必须是字符串，得到 {type(text).__name__}")
    frames: list[str] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        body = line.split("#", 1)[0].strip()
        if not body:
            continue
        for token in re.split(r"[\s,]+", body):
            if not token:
                continue
            try:
                frame_hex_len(token)
            except WaveformError as exc:
                raise WaveformError(f"第 {line_no} 行：{exc}") from exc
            frames.append(token)
    if not frames:
        raise WaveformError(f"波形 {name!r} 的文本里没有任何帧（只有注释/空行？）")
    return _build(name, frames, None, "imported")


def import_file(path: str | Path, *, source: str = "imported") -> Waveform:
    """按扩展名导入：``.json`` 走 :func:`import_json`，其他当纯文本。"""
    file = Path(path)
    if not file.is_file():
        raise WaveformError(f"波形文件不存在：{file}")
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise WaveformError(f"波形文件读取失败：{file}（{exc}）") from exc
    if file.suffix.lower() == ".json":
        return import_json(text, file.stem, source=source)
    return import_text(text, file.stem)


# ──────────────────────────────────────────────────────────────
# 转换（**有损**，只保证可确定的部分 —— 见模块 docstring）
# ──────────────────────────────────────────────────────────────
def _v2_x_for_frequency(frequency: int) -> int:
    """官方 v2 文档的推荐拆分：``X = ((Frequency/1000)^0.5) * 15``，``Y = Frequency - X``。

    注意：这**不是**官方 16 条向量用过的那套拆分（官方向量里同一个 Frequency
    对应多个 X），所以 V3→V2 只能保证 ``X + Y == Frequency``，不能保证与官方向量逐字节相同。
    """
    x = int(math.sqrt(frequency / 1000) * 15)
    return max(1, min(V2_X_MAX, x))


def to_v2(frames: Sequence[str]) -> list[str]:
    """V3 帧 → V2 帧（**有损**）：``Z = 强度 // 5``，``X + Y = 频率``。

    - 4 组不相同（非官方常见情形）时取第 1 组，并记一条 debug 日志；
    - 强度维度精确可逆（``to_v3(to_v2(x)) 的强度 == 原强度`` 当强度是 5 的倍数）；
    - 频率维度可逆（``X + Y == 频率``），但 X/Y 的**拆分**用的是文档推荐公式，
      与官方向量的拆分可能不同（同一个频率对应多个 X，信息已丢失）。
    """
    frames = normalize_frames(frames)
    version = detect_version(frames)
    if version == 2:
        return list(frames)
    out: list[str] = []
    for index, frame in enumerate(frames):
        groups = decode_v3_frame(frame)
        if len(set(groups)) != 1:
            logger.debug("第 %d 帧的 4 组不相同，转 V2 时只保留第 1 组：%s", index + 1, groups)
        frequency, strength = groups[0]
        frequency = max(FREQ_MIN, min(FREQ_MAX, frequency))
        x = _v2_x_for_frequency(frequency)
        y = max(0, min(V2_Y_MAX, frequency - x))
        z = min(V2_Z_MAX, max(0, strength // STRENGTH_PER_Z))
        out.append(encode_v2_frame(x, y, z))
    return out


def to_v3(frames: Sequence[str]) -> list[str]:
    """V2 帧 → V3 帧（**有损**）：4 组相同，``频率 = X + Y``，``强度 = Z * 5``。

    - ``X=0 && Y=0``（官方向量里的静音帧 ``"000000"``）→ 频率 :data:`SILENT_FREQ`、强度 0；
    - 强度反推只能到 ``Z * 5``：官方 V2 的 Z 是 ``强度 // 5`` 的结果，
      原强度落在 ``[5Z, 5Z+4]``，所以这一步**天然不可能**与 ``expectedV3`` 逐字节相同
      （官方向量的 V2/V3 是同一 raw 的两种独立编码，不是转换对）。
    """
    frames = normalize_frames(frames)
    version = detect_version(frames)
    if version == 3:
        return list(frames)
    out: list[str] = []
    for index, frame in enumerate(frames):
        params = decode_v2_frame(frame)
        if params.is_silent:
            frequency = SILENT_FREQ
            strength = 0
        else:
            frequency = params.frequency
            if frequency > FREQ_MAX:
                logger.debug(
                    "第 %d 帧的 X+Y=%d 超过 V3 频率上限 %d，已截断",
                    index + 1,
                    frequency,
                    FREQ_MAX,
                )
                frequency = FREQ_MAX
            frequency = max(FREQ_MIN, frequency)
            strength = min(STRENGTH_MAX, params.z * STRENGTH_PER_Z)
        out.append(encode_v3_frame([(frequency, strength)] * 4))
    return out


# ──────────────────────────────────────────────────────────────
# 官方波形 + 导入波形 → 一张查找表
# ──────────────────────────────────────────────────────────────
def official_waveforms(kind: str = "coyote") -> dict[str, Waveform]:
    """官方波形库（**数量运行时读取，绝不硬编码**）。

    ``kind="coyote"`` 取 ``COYOTE_WAVEFORMS``（郊狼），``"ovc"`` 取 ``OVC_WAVEFORMS``
    （负鼠/震动控制器）。SDK 未安装时报错而不是静默返回空表。
    """
    if kind not in ("coyote", "ovc"):
        raise WaveformError(f"kind 只能是 'coyote' 或 'ovc'，得到 {kind!r}")
    try:  # 延迟导入：让 waveform_io 在没装 SDK 时也能被单测（可注入 official=）
        from dglab_kit_python import COYOTE_WAVEFORMS, OVC_WAVEFORMS
    except ImportError as exc:  # pragma: no cover - 环境问题，不是逻辑问题
        raise WaveformError(
            "未安装官方 SDK dglab-kit-python，无法读取官方波形；"
            "安装：pip install --proxy http://127.0.0.1:7897 --index-url https://pypi.org/simple "
            '"reference/dglab-kit-python"'
        ) from exc

    table = COYOTE_WAVEFORMS if kind == "coyote" else OVC_WAVEFORMS
    out: dict[str, Waveform] = {}
    for key, entry in table.items():  # type: ignore[union-attr]
        name = str(getattr(key, "value", key)).upper()
        frames = entry["raw"]  # type: ignore[index]
        label = entry.get("label") or {}  # type: ignore[union-attr]
        aliases = tuple(str(v) for v in (label.get("cn"), label.get("en")) if v)
        out[name] = Waveform(
            name=name,
            frames=normalize_frames(frames),  # type: ignore[arg-type]
            version=3,
            source="official",
            aliases=aliases,
        )
    return out


def load_all(
    data_dir: str | Path,
    *,
    official: dict[str, Waveform] | None = None,
    pattern: str = "*.json",
) -> dict[str, Waveform]:
    """加载 ``data_dir`` 下导入的波形，与官方波形**合并成一张查找表**。

    规则（任务书 §4.6.5）：

    - **官方波形优先**，重名**给出 warning，不静默覆盖**（导入的那条被跳过）；
    - 导入的波形之间重名同样 warning + 先到先得；
    - 单个文件坏掉（非法 hex / JSON 错）只 warning 跳过，**不打死节点**。

    返回的键是**规范化后的名字**（去空白 + 大写）；用 :func:`find` 做大小写/别名查询。
    """
    directory = Path(data_dir)
    table: dict[str, Waveform] = {}
    for name, wave in (official if official is not None else official_waveforms()).items():
        table[name.strip().upper()] = wave

    if directory.is_dir():
        for file in sorted(directory.glob(pattern)):
            try:
                wave = import_file(file)
            except WaveformError as exc:
                logger.warning("跳过无法导入的波形文件 %s：%s", file, exc)
                continue
            key = wave.name.strip().upper()
            if key in table:
                existing = table[key]
                logger.warning(
                    "波形重名：%s（来源 %s）与已加载的（来源 %s）冲突，保留已加载的那条，"
                    "跳过 %s",
                    wave.name,
                    wave.source,
                    existing.source,
                    file,
                )
                continue
            table[key] = wave
    elif not directory.exists():
        logger.debug("波形目录不存在，只用官方波形：%s", directory)

    return table


def find(table: dict[str, Waveform], name: str) -> Waveform | None:
    """在查找表里找波形：先精确（大小写不敏感），再按别名（中/英文 label）。"""
    if not isinstance(name, str) or not name.strip():
        return None
    key = name.strip().upper()
    if key in table:
        return table[key]
    for wave in table.values():
        if any(key == alias.strip().upper() for alias in wave.aliases):
            return wave
    return None
