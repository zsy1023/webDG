"""``python -m agent.nodes.coyote_v4`` 入口。

supervisor（``agent/nodes.toml`` 的默认 ``module = "agent.nodes.<name>"``）
与手工调试都用这个入口起一个进程。
"""

from __future__ import annotations

from . import CoyoteV4Node

if __name__ == "__main__":
    raise SystemExit(CoyoteV4Node.main())
