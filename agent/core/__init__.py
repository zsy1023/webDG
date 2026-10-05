"""CoyoteLink 节点框架（TF 的产物，**其他任务只读**）。

目标形态（任务书 §0）：让"写一个节点"只需三步：

    from agent.core import Node, topics

    class PoseNode(Node):
        name = "pose"

        async def setup(self) -> None:
            self.sub = self.subscribe(topics.FEATURE_FRAME_RAW, self.on_frame)
            self.pub = self.publish(topics.FEATURE_POSE)

        async def on_frame(self, msg: dict) -> None:
            ...

    if __name__ == "__main__":
        raise SystemExit(PoseNode.main())

模块地图：

| 模块 | 职责 |
|---|---|
| `node.py` | `Node` 基类：session / pub / sub / queryable / 生命周期 / 优雅退出 |
| `topics.py` | ★ **key space 唯一真源**（§5.3 表格落地成 `KEY_SPECS`） |
| `messages.py` | 消息 schema（`Message` 基类 + `TOPIC_MESSAGES` 注册表） |
| `config.py` | 配置加载 + **UTF-8 强制** |
| `logging.py` | 统一日志（UTF-8 FileHandler） |
| `devices.py` | M0 设备管理（⚠️ **T2 独占**，此处仅桩） |
| `supervisor.py` | 进程管理 + 指数退避重启 |
| `cli.py` | `doctor` / `nodes` / `run` / `run-all` / `pub` / `sub` |
"""

from __future__ import annotations

from . import config, devices, logging, messages, topics
from .config import Config, ConfigError, force_utf8, normalize_level
from .logging import get_logger, setup_logging
from .node import (
    Node,
    NodeError,
    Publisher,
    Query,
    Queryable,
    Subscriber,
    run_node,
)

__all__ = [
    # 核心
    "Node",
    "run_node",
    "NodeError",
    "Publisher",
    "Subscriber",
    "Queryable",
    "Query",
    # 配置与日志
    "Config",
    "ConfigError",
    "force_utf8",
    "normalize_level",
    "setup_logging",
    "get_logger",
    # 子模块（节点只读，不要改）
    "topics",
    "messages",
    "config",
    "logging",
    "devices",
]

__version__ = "3.0.0"
