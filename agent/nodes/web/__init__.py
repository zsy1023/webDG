"""``web`` 节点（T5）—— 面板后端：把 Zenoh 上的全部 topic 转成 WebSocket 推给浏览器。

- 订阅 ``coyotelink/<room>/**``（``topics.all_keys``）—— **全部** topic
- 把每条消息**逐条**转成浏览器能直接吃的 JSON，经 ``/ws`` 广播
- 浏览器发指令（开火 / 急停 / 选设备 / 波形导入）→ 翻译成对应的 ``command/*`` 发回 Zenoh

隐私（需求 1d）：这里只搬结构化 JSON（坐标 / 角度 / 音量 / 事件），
``feature/frame.raw`` 的原始像素**从未进入**本节点 —— 浏览器拿到的永远没有图像或音频字节。

单跑调试（**必须** uvicorn.run 启动，见 :mod:`.node` 头部说明）::

    python -m agent.nodes.web                    # http://127.0.0.1:8780
    python -m agent.nodes.web --port 9000
    python -m agent.core.cli run web             # 由框架驱动（不带 --port，用默认端口）
"""

from __future__ import annotations

from .node import NODE, WebNode

__all__ = ["WebNode", "NODE"]
