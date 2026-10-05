"""``python -m agent.nodes.heart`` 入口。

    python -m agent.nodes.heart
    python -m agent.core.cli run heart
"""

from __future__ import annotations

from agent.nodes.heart.node import HeartNode

if __name__ == "__main__":
    raise SystemExit(HeartNode.main())
