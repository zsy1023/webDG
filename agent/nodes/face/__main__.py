"""``python -m agent.nodes.face`` 入口（supervisor 用）。"""

from __future__ import annotations

from . import FaceNode

if __name__ == "__main__":
    raise SystemExit(FaceNode.main())
