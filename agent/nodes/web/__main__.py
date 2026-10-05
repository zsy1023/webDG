"""``python -m agent.nodes.web`` 入口（supervisor 用）。

注意：本节点**必须**用 ``uvicorn.run()`` 启动（见 :mod:`.node` 头部说明），
因此它覆盖了基类的 ``main()`` 以接收 ``--host`` / ``--port``。
不带参数时用默认端口 8780 —— supervisor 就是这样起的。
"""

from __future__ import annotations

from . import WebNode

if __name__ == "__main__":
    raise SystemExit(WebNode.main())
