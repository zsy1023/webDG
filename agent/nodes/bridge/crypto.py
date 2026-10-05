"""可选的应用层 AEAD（端到端加密）—— T3 收缩范围第 3 条。

**职责边界（务必读）**：

* Zenoh 自己的 ``transport/link/tls`` 是**链路加密** —— 保护"这一段连接"；
* 本模块是**应用层 E2E** —— 连**中继（zenohd router）也看不到明文**，
  因为密文在两端各自加解密，中继只转发密文。两者可叠加。

因此它**默认关闭**（``[bridge] encrypt = false`` / ``--encrypt`` 打开）：
跨机链路的机密性**首要**由 Zenoh TLS 保障；E2E AEAD 是"连中继都不信任"时的加固。
（T3 旧版要求"加密默认开"，那是针对自研明文传输；改用 Zenoh 后链路本身已可加密，
 AEAD 降级为可选加固，故默认关闭。）

密钥派生（任务书 §4.4 的"最低要求"档）：

    key = HKDF-SHA256(ikm = 房间码, salt = b"coyotelink-bridge-v1", info = b"coyotelink-v1")

即"房间码即口令"，安全性依赖房间码不外泄；两端用**同一个房间码**得到**同一把钥匙**，
无需额外握手（简单、可测）。每条消息的 AAD 用它的 key 后缀，防止跨 key 重放。

密文线格式：``MAGIC(4) || nonce(12) || AES-256-GCM ciphertext+tag``。
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

__all__ = [
    "MAGIC",
    "NONCE_LEN",
    "KEY_LEN",
    "SealError",
    "derive_key",
    "seal",
    "open_sealed",
]

#: 线格式魔数，用于快速识别"这是本模块加的密文"
MAGIC = b"CLB1"
#: AES-GCM nonce 长度
NONCE_LEN = 12
#: AES-256 密钥长度
KEY_LEN = 32
#: HKDF 默认参数（两端必须一致）
DEFAULT_SALT = b"coyotelink-bridge-v1"
DEFAULT_INFO = b"coyotelink-v1"


class SealError(ValueError):
    """解密失败（魔数不符 / 密文损坏 / 认证标签不匹配 / 密钥不对）。"""


def derive_key(
    room_code: str,
    *,
    salt: bytes = DEFAULT_SALT,
    info: bytes = DEFAULT_INFO,
) -> bytes:
    """房间码 → 32 字节会话密钥（HKDF-SHA256，确定性）。"""
    if not room_code:
        raise SealError("房间码不能为空")
    hkdf = HKDF(algorithm=hashes.SHA256(), length=KEY_LEN, salt=salt, info=info)
    return hkdf.derive(room_code.encode("utf-8"))


def seal(key: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    """加密：``plaintext`` → ``MAGIC || nonce || ct``。

    :param key: :func:`derive_key` 的产物（32 字节）
    :param aad: 附加认证数据（这里传 key 后缀），不加密但参与完整性校验
    """
    if len(key) != KEY_LEN:
        raise SealError(f"密钥必须 {KEY_LEN} 字节，得到 {len(key)} 字节")
    nonce = os.urandom(NONCE_LEN)
    ciphertext = AESGCM(key).encrypt(nonce, bytes(plaintext), bytes(aad))
    return MAGIC + nonce + ciphertext


def open_sealed(key: bytes, blob: bytes, aad: bytes = b"") -> bytes:
    """解密 :func:`seal` 的产物；任何问题都抛 :class:`SealError`。"""
    if len(key) != KEY_LEN:
        raise SealError(f"密钥必须 {KEY_LEN} 字节，得到 {len(key)} 字节")
    data = bytes(blob)
    if not data.startswith(MAGIC):
        raise SealError("缺少魔数前缀，可能对端未加密或数据被改")
    body = data[len(MAGIC):]
    if len(body) < NONCE_LEN:
        raise SealError("密文过短")
    nonce, ciphertext = body[:NONCE_LEN], body[NONCE_LEN:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, bytes(aad))
    except InvalidTag as exc:
        raise SealError(f"AEAD 认证失败（密钥或数据/AAD 不对）：{exc}") from exc
