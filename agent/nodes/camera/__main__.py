"""``python -m agent.nodes.camera`` 入口（supervisor 用）。"""

from __future__ import annotations

from . import CameraNode

if __name__ == "__main__":
    raise SystemExit(CameraNode.main())
