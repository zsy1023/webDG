"""``bridge`` 节点（T3）—— 跨机白名单桥接。

对外导出：

* :class:`BridgeNode` —— 节点本体（白名单转发 + 可选 E2E 加密 + 握手）；
* :data:`NODE` —— supervisor / ``cli run`` 用的默认工厂（同 :class:`BridgeNode`）；
* :mod:`agent.nodes.bridge.room` —— 房间码与角色约定；
* :mod:`agent.nodes.bridge.crypto` —— 可选的应用层 AEAD；
* :mod:`agent.nodes.bridge.selftest` —— 同机双进程端到端实测（**证据生成器**）。

单跑调试::

    python -m agent.nodes.bridge --role a --room-code AB12CD --listen tcp/0.0.0.0:7451
"""

from __future__ import annotations

from . import crypto, room
from .node import HELLO_SUFFIX, LINK_LAYER, NODE, BridgeNode

__all__ = [
    "BridgeNode",
    "NODE",
    "LINK_LAYER",
    "HELLO_SUFFIX",
    "room",
    "crypto",
]
