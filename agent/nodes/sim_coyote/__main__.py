"""``python -m agent.nodes.sim_coyote`` 入口。

默认后端是 ``esp32``（现行路线）；没有硬件时用 ``loopback`` 自测::

    python -m agent.nodes.sim_coyote
    python -m agent.core.cli run sim_coyote
"""

from __future__ import annotations

from agent.nodes.sim_coyote.node import SimCoyoteNode

if __name__ == "__main__":
    raise SystemExit(SimCoyoteNode.main())
