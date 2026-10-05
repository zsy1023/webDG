"""``python -m agent.nodes.bridge`` 入口（supervisor 用）。

参数见 :meth:`agent.nodes.bridge.node.BridgeNode.main`：

    python -m agent.nodes.bridge --role b --room-code AB12CD --connect tcp/<对端>:7451
"""

from __future__ import annotations

from . import BridgeNode

if __name__ == "__main__":
    raise SystemExit(BridgeNode.main())
