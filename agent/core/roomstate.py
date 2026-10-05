"""面板选中的**房间**落盘（`<data_dir>/room.json`）。

和 `data/devices.json`（设备选择）同一套路：面板上选过的东西要**记得住**，
下次起节点不用再填一遍。

优先级（高 → 低）::

    --room <code>            # 命令行显式指定（脚本 / 测试 / 排障用）
    <data_dir>/room.json     # 面板上选过的（本模块）
    agent/config.toml: room  # 配置文件里的默认值

为什么不是"直接写进 config.toml"：那是**人手工编辑**的文件，程序往里写会把用户的
注释和排版冲掉；而且要区分"用户改的"和"面板选的"也说不清。所以单独放一个运行时
状态文件（`data/` 整个不进版本控制，见 `.gitignore`）——和 `data/devices.json` 一致。

`write_room()` 会先读一遍比较，**内容没变就不写**：一次换房间会有十几个节点
（外加对端机器）各调一次，没必要写十几次盘。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

__all__ = ["ROOM_FILE", "room_file", "read_room", "write_room", "clear_room"]

#: 文件名（放在 `Config.data_dir` 下）
ROOM_FILE = "room.json"


def room_file(data_dir: str | Path) -> Path:
    """`<data_dir>/room.json`。"""
    return Path(data_dir) / ROOM_FILE


def read_room(data_dir: str | Path) -> str | None:
    """读面板选过的房间；没有 / 坏了 → ``None``（**不抛**，坏文件不该拦住启动）。"""
    try:
        data = json.loads(room_file(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    room = data.get("room") if isinstance(data, dict) else None
    if not isinstance(room, str) or not room.strip():
        return None
    return room.strip()


def write_room(data_dir: str | Path, room: str) -> bool:
    """记下房间；返回是否真的写了盘（内容没变就跳过）。"""
    room = str(room or "").strip()
    if not room:
        return False
    if read_room(data_dir) == room:
        return False
    path = room_file(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"room": room, "updated_at": int(time.time() * 1000)}
        path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        return False          # 写不进去也不该影响换房间本身（内存里已经切了）
    return True


def clear_room(data_dir: str | Path) -> None:
    """忘掉面板选过的房间（回到 `config.toml` 的默认值）。"""
    try:
        room_file(data_dir).unlink()
    except OSError:
        pass
