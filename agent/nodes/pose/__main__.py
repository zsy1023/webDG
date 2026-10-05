"""``python -m agent.nodes.pose`` 入口（supervisor 用）。"""

from __future__ import annotations

from . import PoseNode

if __name__ == "__main__":
    raise SystemExit(PoseNode.main())
