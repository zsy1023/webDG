"""房间语义 —— 6 位房间码 + A/B 角色约定（T3）。

来源：``docs/tasks/T3-房间与传输层.md`` §4.3「房间码」。

* 房间码 **6 位**，字符集 ``23456789ABCDEFGHJKLMNPQRSTUVWXYZ``（31 字符，去掉
  易混的 ``0 O 1 I L``）→ 31**6 ≈ 8.9 亿组合；
* 用 :func:`secrets.choice` 生成（**不是**随机密钥；它是"配对口令"，密钥由它派生）；
* 角色只有 ``"a"`` / ``"b"`` 两个，**可互换**：谁建房谁是 ``a``，谁加入谁是 ``b``；
  业务角色（控制方/被控方）与它无关，那是上层的事。

本模块只做"字符串层"的约定与校验，不碰网络。房间码 + 角色一起喂给
:class:`agent.nodes.bridge.node.BridgeNode`。
"""

from __future__ import annotations

import secrets

__all__ = [
    "ALPHABET",
    "CODE_LEN",
    "ROLES",
    "RoomCodeError",
    "generate_code",
    "normalize_code",
    "validate_role",
    "peer_role",
]

#: 房间码字符集：数字 2~9 + 大写字母去掉 I / L / O（共 31 个）
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # 去 0/O/1/I/L（易混），共 31 个
#: 房间码长度
CODE_LEN = 6
#: 允许的两个角色
ROLES: tuple[str, ...] = ("a", "b")

_ALLOWED = frozenset(ALPHABET)


class RoomCodeError(ValueError):
    """房间码或角色不合法。"""


def generate_code() -> str:
    """生成一个 6 位随机房间码（``secrets`` 级随机，非密钥用途）。"""
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))


def normalize_code(code: str) -> str:
    """校验并规整房间码（去空白 + 转大写），非法则 :class:`RoomCodeError`。

    用户手输时常见的是大小写混输 / 前后空格，这里统一接受；
    但 **字符与长度必须严格**（否则说明敲错了，直接报错好过静默走偏）。
    """
    if not isinstance(code, str):
        raise RoomCodeError(f"房间码必须是字符串，得到 {type(code).__name__}")
    text = code.strip().upper()
    if len(text) != CODE_LEN:
        raise RoomCodeError(f"房间码必须是 {CODE_LEN} 位，得到 {len(text)} 位：{code!r}")
    bad = sorted(set(text) - _ALLOWED)
    if bad:
        raise RoomCodeError(
            f"房间码含非法字符 {bad}（允许：{ALPHABET}）：{code!r}"
        )
    return text


def validate_role(role: str) -> str:
    """校验并规整角色（转小写）；非法则 :class:`RoomCodeError`。"""
    text = str(role).strip().lower()
    if text not in ROLES:
        raise RoomCodeError(f"角色必须是 {ROLES} 之一，得到 {role!r}")
    return text


def peer_role(role: str) -> str:
    """本端角色 → 对端角色。"""
    return "b" if validate_role(role) == "a" else "a"
