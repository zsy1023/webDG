"""配置加载 + **UTF-8 强制**。

来源：`docs/tasks/TF-节点框架与消息总线.md` §4.4。

本模块有两个职责：

1. 把工作区路径、房间名、日志级别、Zenoh endpoint 收敛成一个 `Config` 对象；
2. **在进程入口强制 stdout/stderr 走 UTF-8** —— 否则设备名里的 `®` / 中文
   会炸 GBK 控制台（见 `T2-姿态感知与骨骼可视化.md` §6.5 坑 3）。

配置文件 `agent/config.toml` 是**可选**的，缺省走默认值；显式传入的路径不存在则报错。
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.core import roomstate

# —— 环境变量覆盖（任务书 §4.4 点名前两个，后三个是本实现的便利扩展）——
ENV_ROOM = "COYOTELINK_ROOM"
ENV_LOG_LEVEL = "COYOTELINK_LOG_LEVEL"
ENV_ROOT = "COYOTELINK_ROOT"
ENV_LOG_DIR = "COYOTELINK_LOG_DIR"
ENV_HEARTBEAT = "COYOTELINK_HEARTBEAT"

DEFAULT_ROOM = "local"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_HEARTBEAT_S = 5.0

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(ValueError):
    """配置有问题（缺文件 / TOML 语法错 / 字段类型错）。"""


def default_project_root() -> Path:
    """工作区根目录（`agent/core/config.py` 往上三层）。"""
    return Path(__file__).resolve().parents[2]


# ──────────────────────────────────────────────────────────────
# UTF-8 强制
# ──────────────────────────────────────────────────────────────
def _reconfigure_stream(stream: Any) -> bool:
    """把单个流重设为 UTF-8；流不支持 `reconfigure` 时安全跳过。"""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return False
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError, TypeError, AttributeError):
        return False
    return True


def force_utf8() -> list[str]:
    """强制 `sys.stdout` / `sys.stderr` 为 UTF-8（`errors="replace"`）。

    幂等；流没有 `reconfigure`（例如 pytest 的某些 capture 对象）时静默跳过。
    返回被成功重设的流名列表，便于测试断言。
    """
    changed: list[str] = []
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None and _reconfigure_stream(stream):
            changed.append(name)
    return changed


def normalize_level(level: str | int) -> str:
    """把日志级别规整成大写字符串；非法值报错（不静默降级）。"""
    if isinstance(level, int):
        return level  # type: ignore[return-value]
    text = str(level).strip().upper()
    if text not in LOG_LEVELS:
        raise ConfigError(f"非法日志级别 {level!r}；可选：{'/'.join(LOG_LEVELS)}")
    return text


# ──────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────
@dataclass
class Config:
    """一个进程一份配置。**不要做模块级全局单例**（任务书 §4.2 要求 5）。"""

    room: str = DEFAULT_ROOM
    project_root: Path = field(default_factory=default_project_root)
    # 下面三个在 __post_init__ 里保证是 Path（构造时可传 None 走默认）
    models_dir: Path = field(default=None)  # type: ignore[assignment]
    data_dir: Path = field(default=None)  # type: ignore[assignment]
    log_dir: Path = field(default=None)  # type: ignore[assignment]
    log_level: str = DEFAULT_LOG_LEVEL
    zenoh: dict[str, Any] = field(default_factory=dict)
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_S
    teardown_timeout_s: float = 10.0
    source: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.project_root = Path(self.project_root).resolve()
        if not self.room:
            self.room = DEFAULT_ROOM
        self.models_dir = self._abs(self.models_dir, "models")
        self.data_dir = self._abs(self.data_dir, "data")
        self.log_dir = self._abs(self.log_dir, "logs")
        self.log_level = normalize_level(self.log_level)
        if not isinstance(self.zenoh, dict):
            raise ConfigError(f"[zenoh] 必须是表（table），得到 {type(self.zenoh).__name__}")
        self.heartbeat_interval_s = float(self.heartbeat_interval_s)
        if self.heartbeat_interval_s < 0:
            raise ConfigError(f"heartbeat_interval_s 不能为负：{self.heartbeat_interval_s}")

    def _abs(self, value: str | Path | None, fallback: str) -> Path:
        if value is None:
            return (self.project_root / fallback).resolve()
        path = Path(value)
        if not path.is_absolute():
            path = self.project_root / path
        return path.resolve()

    # —— 目录 ——
    def ensure_dirs(self) -> None:
        """按需创建 log/data 目录（models 由下载脚本负责，不自动建）。"""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # —— Zenoh ——
    def zenoh_json5(self) -> dict[str, str]:
        """返回 `{json5 键: json5 值}`，由 `node.py` 喂给 `zenoh.Config.insert_json5`。

        支持 `[zenoh]` 下的 `connect` / `listen`（列表）以及 `raw_json5`（原样透传的表）。
        本任务只做本机；跨机 endpoint 归 T3。
        """
        out: dict[str, str] = {}
        connect = self.zenoh.get("connect")
        if connect:
            out["connect/endpoints"] = json.dumps(list(connect))
        listen = self.zenoh.get("listen")
        if listen:
            out["listen/endpoints"] = json.dumps(list(listen))
        # ★ 默认**关掉共享内存（SHM）传输**：
        #   本机 12 个节点之间的 pub/sub 实测"时通时不通" —— 面板发的开火 / 规则 /
        #   选设备经常到不了兄弟节点（`web.log` 明明写了已 publish，目标节点一条没收到），
        #   而心跳 / 订阅方向又基本正常，症状就是那句"点了没反应、状态不更新"。
        #   关掉 SHM（同机对等体改走回环 TCP）后实测 5/5 稳定送达，开着是 0/5。
        #   跨机链路本来就走 TCP，不受影响；想抓 SHM 现场就在
        #   `[zenoh.raw_json5]` 里自己写回 `"transport/shared_memory/enabled" = true` 覆盖。
        out["transport/shared_memory/enabled"] = "false"
        raw_json5 = self.zenoh.get("raw_json5") or {}
        if not isinstance(raw_json5, dict):
            raise ConfigError("[zenoh.raw_json5] 必须是表（table）")
        for key, value in raw_json5.items():
            out[str(key)] = value if isinstance(value, str) else json.dumps(value)
        return out

    # —— 加载 ——
    @classmethod
    def load(cls, path: str | Path | None = None, *, utf8: bool = True) -> "Config":
        """加载配置。

        - `path=None`：读 `<project_root>/agent/config.toml`；**不存在就用默认值**（可选文件）
        - `path` 显式给出：**不存在就报错**（避免"以为读了配置其实没读"）
        - `utf8=True`：顺手执行 `force_utf8()`（任务书 §4.2 要求 3 的"入口处强制"）
        """
        if utf8:
            force_utf8()

        root = Path(os.environ.get(ENV_ROOT) or default_project_root()).resolve()
        explicit = path is not None
        cfg_path = Path(path) if explicit else root / "agent" / "config.toml"
        if not cfg_path.is_absolute():
            cfg_path = (root / cfg_path).resolve()

        data: dict[str, Any] = {}
        if cfg_path.exists():
            try:
                with cfg_path.open("rb") as fh:
                    data = tomllib.load(fh)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"配置文件解析失败：{cfg_path}\n  {exc}") from exc
            except OSError as exc:
                raise ConfigError(f"配置文件读取失败：{cfg_path}\n  {exc}") from exc
        elif explicit:
            raise ConfigError(
                f"配置文件不存在：{cfg_path}\n"
                f"  （显式指定的路径必须存在；不指定则读 {root / 'agent' / 'config.toml'}，缺失时用默认值）"
            )

        if not isinstance(data, dict):
            raise ConfigError(f"配置文件顶层必须是表（table）：{cfg_path}")

        paths = data.get("paths") or {}
        if not isinstance(paths, dict):
            raise ConfigError(f"[paths] 必须是表（table）：{cfg_path}")

        zenoh = data.get("zenoh") or {}
        if not isinstance(zenoh, dict):
            raise ConfigError(f"[zenoh] 必须是表（table）：{cfg_path}")

        # —— 房间的优先序：ENV_ROOM > <data_dir>/room.json（面板上选过的） > config.toml ——
        #    （其余字段：环境变量最高，便于脚本/测试临时覆盖）
        data_dir = paths.get("data")
        data_dir = (
            (root / data_dir).resolve() if data_dir and not Path(data_dir).is_absolute()
            else (Path(data_dir).resolve() if data_dir else (root / "data").resolve())
        )
        room = (
            os.environ.get(ENV_ROOM)
            or roomstate.read_room(data_dir)
            or data.get("room")
            or DEFAULT_ROOM
        )
        log_level = os.environ.get(ENV_LOG_LEVEL) or data.get("log_level") or DEFAULT_LOG_LEVEL
        heartbeat = data.get("heartbeat_interval_s", DEFAULT_HEARTBEAT_S)
        if ENV_HEARTBEAT in os.environ:
            heartbeat = os.environ[ENV_HEARTBEAT]
        log_dir = os.environ.get(ENV_LOG_DIR) or paths.get("logs")

        try:
            return cls(
                room=str(room),
                project_root=root,
                models_dir=paths.get("models"),
                data_dir=paths.get("data"),
                log_dir=log_dir,
                log_level=log_level,
                zenoh=zenoh,
                heartbeat_interval_s=float(heartbeat),
                teardown_timeout_s=float(data.get("teardown_timeout_s", 10.0)),
                source=cfg_path if cfg_path.exists() else None,
                raw=data,
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ConfigError):
                raise
            raise ConfigError(f"配置字段类型错误（{cfg_path}）：{exc}") from exc
