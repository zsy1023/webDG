"""``python -m agent.nodes.audio`` 入口（supervisor 用）。"""

from __future__ import annotations

from . import AudioNode

if __name__ == "__main__":
    raise SystemExit(AudioNode.main())
