"""``python -m agent.nodes.device`` 入口（supervisor 用）。"""

from __future__ import annotations

from . import DeviceNode

if __name__ == "__main__":
    raise SystemExit(DeviceNode.main())
