"""``feature/pose`` 的紧凑编解码 —— **单帧序列化 ≤ 300 字节**。

为什么不能"直接发浮点"
----------------------
33 个点 × 3 个坐标，若按 JSON 浮点写（``[0.512,0.334,0.021]``），一帧要 **600+ 字节**，
远超任务书 §5.1 的 300 字节预算。所以先**量化**再**打包**：

- ``x`` / ``y`` → ``uint16``（精度 ``1/65535``，肉眼无差别）；
- ``z``（相对深度，MediaPipe 里通常很小）→ ``uint8``，量程 ``[-0.5, 0.5]``；
- ``visibility`` → ``uint8``。

打包后按 **base85** 编码成一段 ASCII，塞进 JSON 字符串字段。落盘 / 跨机 / 抓包看到的
**始终是纯 JSON 文本**（没有图像字节），符合需求 1d。

线格式（wire）
--------------
``present=1``::

    {"t": <int 毫秒时间戳>, "s": <int 序号>, "p": 1, "q": "<base85>"}

``present=0``::

    {"t": ..., "s": ..., "p": 0}

``q`` 解出的字节布局（点数 ``n``，头部 1 字节自描述）：

- 第 0 字节：``n`` 低 7 位 + 最高位 = 有无 ``visibility``（``0x80`` = 有）；
- 之后 ``n*5`` 字节：每点 ``x(uint16 BE) + y(uint16 BE) + z(uint8)``；
- 若有可见度：再 ``n`` 字节，每点一个 ``visibility(uint8)``。

头部让解码不再依赖"字节长度反推"（base85 会补零到 4 字节倍数，长度反推不可靠）。

坐标一律归一化：``x = qx/65535``、``y = qy/65535``、``z = qz/255*1.0 - 0.5``、
``visibility = qv/255``。**协议里没有像素坐标**（任务书 §5 要求 3）。

这个模块**不依赖 numpy / mediapipe**，可被单测、CLI、前端桥接安全导入。
"""

from __future__ import annotations

import base64
import json
import struct
from dataclasses import dataclass, field
from typing import Any, Sequence

__all__ = [
    "CodecError",
    "FeatureFrame",
    "LANDMARK_COUNT",
    "MAX_WIRE_BYTES",
    "Z_RANGE",
    "encode_frame",
    "decode_frame",
    "pack_landmarks",
    "unpack_landmarks",
]

#: BlazePose 关键点数量
LANDMARK_COUNT = 33

#: 单帧字节预算（任务书 §7 B5）
MAX_WIRE_BYTES = 300

#: ``z`` 的量化量程（中心 0，两端 ±）
Z_RANGE = (-0.5, 0.5)

_POINT = struct.Struct(">HHB")  # x uint16, y uint16, z uint8
_PAYLOAD_HEADER = struct.Struct(">B")  # n(低7位) | has_visibility(0x80)
_MAX_POINTS = 127
_XY_SCALE = 65535.0
_VIS_SCALE = 255.0
_Z_LO, _Z_HI = Z_RANGE


class CodecError(ValueError):
    """payload 与线格式对不上（非法 JSON / 非法 base85 / 长度不对）。"""


# ──────────────────────────────────────────────────────────────
# 量化（纯函数，便于单测）
# ──────────────────────────────────────────────────────────────
def _clamp01(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _quant_xy(value: float) -> int:
    return int(round(_clamp01(float(value)) * _XY_SCALE))


def _dequant_xy(value: int) -> float:
    return value / _XY_SCALE


def _quant_z(value: float) -> int:
    span = _Z_HI - _Z_LO
    t = (float(value) - _Z_LO) / span
    return int(round(_clamp01(t) * _VIS_SCALE))


def _dequant_z(value: int) -> float:
    return value / _VIS_SCALE * (_Z_HI - _Z_LO) + _Z_LO


def _quant_vis(value: float) -> int:
    return int(round(_clamp01(float(value)) * _VIS_SCALE))


def _dequant_vis(value: int) -> float:
    return value / _VIS_SCALE


# ──────────────────────────────────────────────────────────────
# 打包 / 解包
# ──────────────────────────────────────────────────────────────
def _b85_pad(raw: bytes) -> bytes:
    """把字节数补到 4 的倍数 —— 让 base85 输出长度确定（无尾组，恒为 ``len/4*5``）。"""
    remainder = len(raw) % 4
    if remainder:
        raw = raw + b"\x00" * (4 - remainder)
    return raw


def pack_landmarks(
    landmarks: Sequence[Sequence[float]],
    visibility: Sequence[float] | None = None,
) -> str:
    """``landmarks``(+``visibility``) → base85 字符串（``q`` 字段的值）。

    首字节自描述点数与有无可见度，因此解码不必靠长度猜测。
    """
    count = len(landmarks)
    if count == 0:
        raise CodecError("landmarks 为空，无法打包")
    if count > _MAX_POINTS:
        raise CodecError(f"关键点数量 {count} 超过 {_MAX_POINTS}，需要扩展头部格式")
    has_visibility = visibility is not None
    chunks: list[bytes] = [_PAYLOAD_HEADER.pack(count | (0x80 if has_visibility else 0))]
    for point in landmarks:
        x = point[0] if len(point) > 0 else 0.0
        y = point[1] if len(point) > 1 else 0.0
        z = point[2] if len(point) > 2 else 0.0
        chunks.append(_POINT.pack(_quant_xy(x), _quant_xy(y), _quant_z(z)))
    if has_visibility:
        for i in range(count):
            value = visibility[i] if i < len(visibility) else 0.0  # type: ignore[index]
            chunks.append(struct.pack(">B", _quant_vis(value)))
    return base64.b85encode(_b85_pad(b"".join(chunks))).decode("ascii")


def unpack_landmarks(text: str, *, count: int | None = None) -> tuple[list[list[float]], list[float] | None]:
    """``q`` 字段 → ``(landmarks, visibility|None)``。

    点数与有无可见度从**首字节**读出（不再靠长度猜，避免 base85 补零导致误判）；
    ``count`` 给定时做一致性校验。
    """
    if not isinstance(text, str) or not text:
        raise CodecError("q 字段必须是非空字符串")
    try:
        raw = base64.b85decode(text.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise CodecError(f"q 字段不是合法 base85：{exc}") from exc
    if len(raw) < _PAYLOAD_HEADER.size:
        raise CodecError("q 字段太短，缺少头部")

    flag = raw[0]
    points = flag & 0x7F
    has_visibility = bool(flag & 0x80)
    if points == 0:
        raise CodecError("q 字段头部声明了 0 个关键点")
    if count is not None and count != points:
        raise CodecError(f"q 字段含 {points} 个关键点，与期望的 {count} 不符")
    needed = _PAYLOAD_HEADER.size + points * _POINT.size + (points if has_visibility else 0)
    if len(raw) < needed:
        raise CodecError(f"q 字段太短：{len(raw)} 字节，期望至少 {needed}")

    landmarks: list[list[float]] = []
    offset = _PAYLOAD_HEADER.size
    for _ in range(points):
        x, y, z = _POINT.unpack_from(raw, offset)
        landmarks.append([_dequant_xy(x), _dequant_xy(y), _dequant_z(z)])
        offset += _POINT.size
    visibility: list[float] | None = None
    if has_visibility:
        visibility = [_dequant_vis(raw[offset + i]) for i in range(points)]
    return landmarks, visibility


# ──────────────────────────────────────────────────────────────
# FeatureFrame
# ──────────────────────────────────────────────────────────────
@dataclass
class FeatureFrame:
    """一帧姿态特征（``feature/pose`` 的 payload）。坐标一律归一化 0~1。"""

    ts: int
    seq: int
    present: bool
    landmarks: list[list[float]] = field(default_factory=list)
    visibility: list[float] | None = None

    def to_wire(self) -> dict[str, Any]:
        """紧凑 dict（JSON-friendly），**只有 t/s/p/q 四个字段**。"""
        wire: dict[str, Any] = {
            "t": int(self.ts),
            "s": int(self.seq),
            "p": 1 if self.present else 0,
        }
        if self.present and self.landmarks:
            wire["q"] = pack_landmarks(self.landmarks, self.visibility)
        return wire

    def to_json(self) -> str:
        return json.dumps(self.to_wire(), ensure_ascii=False, separators=(",", ":"))

    def to_bytes(self) -> bytes:
        return self.to_json().encode("utf-8")

    def wire_size(self) -> int:
        return len(self.to_bytes())

    @classmethod
    def from_wire(cls, data: Any) -> "FeatureFrame":
        if data is None:
            raise CodecError("payload 为空")
        if isinstance(data, (bytes, bytearray, memoryview)):
            return cls.from_bytes(bytes(data))
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError as exc:
                raise CodecError(f"payload 不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise CodecError(f"payload 必须是 JSON 对象，得到 {type(data).__name__}")

        ts = int(data.get("t", 0) or 0)
        seq = int(data.get("s", 0) or 0)
        present = bool(data.get("p", 0))
        if not present:
            return cls(ts=ts, seq=seq, present=False, landmarks=[], visibility=None)
        packed = data.get("q")
        if not packed:
            return cls(ts=ts, seq=seq, present=True, landmarks=[], visibility=None)
        landmarks, visibility = unpack_landmarks(packed)
        return cls(ts=ts, seq=seq, present=True, landmarks=landmarks, visibility=visibility)

    @classmethod
    def from_bytes(cls, data: bytes | bytearray | memoryview) -> "FeatureFrame":
        try:
            text = bytes(data).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CodecError(f"payload 不是合法 UTF-8：{exc}") from exc
        return cls.from_wire(text)


# ──────────────────────────────────────────────────────────────
# 顶层便捷函数（帧协议与传输解耦：换 WebRTC 也不动这里）
# ──────────────────────────────────────────────────────────────
def encode_frame(frame: FeatureFrame) -> bytes:
    return frame.to_bytes()


def decode_frame(data: Any) -> FeatureFrame:
    return FeatureFrame.from_wire(data)
