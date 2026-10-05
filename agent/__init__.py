"""CoyoteLink 节点化后端。

本文件是**共享文件**（TF 任务建立，见 `docs/tasks/TF-节点框架与消息总线.md` §4.2 要求 7）：
其他任务**不得修改**。它只负责让 `agent` 成为一个显式包，从而
`python -m agent.core.cli` / `python -m agent.nodes.<name>` 都能稳定工作。

节点框架本体在 `agent.core`，key space 唯一真源在 `agent.core.topics`。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "3.0.0"
