"""命令行入口。

来源：`docs/tasks/TF-节点框架与消息总线.md` §4.7。

    python -m agent.core.cli doctor              # 环境自检（复用 tools/doctor.py）
    python -m agent.core.cli nodes               # 列出 nodes.toml 的节点与状态
    python -m agent.core.cli run pose            # 单跑一个节点（调试用）
    python -m agent.core.cli run-all             # 起 supervisor
    python -m agent.core.cli pub <key> '<json>'  # ★ 手动发一条消息（调试神器）
    python -m agent.core.cli sub <key>           # ★ 订阅并打印（调试神器）
    python -m agent.core.cli topics              # 打印 key space（附加便利）

`pub` / `sub` 的 key 既可以是**完整 key**（形如 `<前缀>/<room>/feature/pose`），
也可以是**后缀**（`feature/pose`，自动按 `--room` 补全）。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import os
import runpy
import sys
from pathlib import Path
from typing import Any, Sequence

from . import topics
from . import logging as logging_mod
from .config import Config, ConfigError, force_utf8, normalize_level
from .messages import to_bytes as encode_payload
from .node import Node, NodeError, run_node
from .supervisor import (
    DEFAULT_NODE_PACKAGE,
    STATE_DISABLED,
    STATE_MISSING,
    Supervisor,
    SupervisorConfig,
    SupervisorError,
    load_config,
    module_exists,
)

__all__ = ["CliError", "main", "build_parser"]


class CliError(RuntimeError):
    """CLI 用法错误（给用户看的清晰报错）。"""


# ──────────────────────────────────────────────────────────────
# CLI 专用小节点
# ──────────────────────────────────────────────────────────────
class _CliSubNode(Node):
    """`cli sub`：订阅并打印。stdout 只放消息，日志走 stderr（方便管道）。"""

    name = "cli-sub"
    heartbeat_interval = 0
    cli_mode = True

    def __init__(self, config: Config, key: str) -> None:
        super().__init__(config)
        self._key = key

    async def setup(self) -> None:
        self.subscribe(self._key, self._on_msg, with_key=True)
        print(f"# 订阅 {topics.resolve(self._key, self.room)}（Ctrl+C 退出）", file=sys.stderr, flush=True)

    def _on_msg(self, key: str, msg: Any) -> None:
        if isinstance(msg, (bytes, bytearray)):
            text = bytes(msg).decode("utf-8", "replace")
        elif isinstance(msg, str):
            text = msg
        else:
            text = json.dumps(msg, ensure_ascii=False)
        print(f"{key}  {text}", flush=True)


class _CliPubNode(Node):
    """`cli pub`：发一条消息就退出。"""

    name = "cli-pub"
    heartbeat_interval = 0
    cli_mode = True

    #: 发布后等多久再关 session（zenoh 异步投递，不等可能丢消息）
    settle_s = 0.5

    def __init__(self, config: Config, key: str, payload: bytes) -> None:
        super().__init__(config)
        self._key = key
        self._payload = payload

    async def setup(self) -> None:
        publisher = self.publish(self._key)
        publisher.put(self._payload)
        # zenoh 的 put 是异步投递；立刻 close session 有丢消息的风险（实测过）。
        # 等一小会儿让发送队列排空，再优雅退出 —— CLI 是一次性命令，这点延迟无所谓。
        await asyncio.sleep(self.settle_s)
        print(f"# 已发布 → {publisher.key}", file=sys.stderr, flush=True)
        self.stop()


# ──────────────────────────────────────────────────────────────
# 子命令
# ──────────────────────────────────────────────────────────────
def cmd_doctor(args: argparse.Namespace) -> int:
    """复用 `tools/doctor.py`（只读，不修改它）。"""
    root = Config.load(utf8=False).project_root
    doctor = root / "tools" / "doctor.py"
    if not doctor.exists():
        raise CliError(f"找不到环境自检脚本：{doctor}")
    try:
        runpy.run_path(str(doctor), run_name="__main__")
    except SystemExit as exc:  # doctor.py 自己 sys.exit(返回值)
        return int(exc.code or 0)
    return 0


def cmd_topics(args: argparse.Namespace) -> int:
    """打印 key space（F9 的人眼核对入口）。"""
    room = args.room or Config.load(utf8=False).room
    print(f"# key space（前缀 {topics.PREFIX}/<room>/，当前 room={room}）")
    print(f"{'key':<46} {'跨机':<6} {'发布者':<12} 订阅者")
    print("-" * 100)
    for spec in topics.KEY_SPECS:
        full = (
            topics.key(spec.suffix, room)
            if spec.suffix != topics.STATUS_PATTERN
            else topics.status("<node>", room)
        )
        mark = "✅" if spec.cross_machine else "❌"
        print(
            f"{full:<46} {mark:<6} {','.join(spec.publishers):<12} {','.join(spec.subscribers)}"
        )
    print()
    print(f"# 跨机白名单（{len(topics.CROSS_MACHINE_ALLOWLIST)} 条）")
    for suffix in topics.CROSS_MACHINE_ALLOWLIST:
        print(f"  {suffix}")
    print()
    print(f"# 节点清单（{len(topics.NODE_NAMES)} 个）：{' '.join(topics.NODE_NAMES)}")
    return 0


def cmd_nodes(args: argparse.Namespace) -> int:
    """列出 `nodes.toml` 的节点与状态。"""
    config = load_config(args.config)
    print(f"# 节点清单：{config.source}")
    print(
        f"# restart={config.restart} backoff={config.backoff_initial_ms}~{config.backoff_max_ms}ms "
        f"max_restarts={config.max_restarts} stop_timeout={config.stop_timeout_ms}ms "
        f"skip_missing={config.skip_missing}"
    )
    print()
    print(f"{'节点':<12} {'启用':<6} {'模块状态':<10} {'重启':<6} 模块")
    print("-" * 96)
    for spec in config.nodes:
        exists = module_exists(spec.target_module, config.pythonpath)
        if not spec.enabled:
            state = "关闭"
        elif exists:
            state = "就绪"
        elif config.skip_missing:
            state = "missing"
        else:
            state = "缺失!"
        restart = "继承" if spec.restart is None else ("是" if spec.restart else "否")
        flag = "" if spec.name in topics.NODE_NAMES else "   ⚠ 不在 §5.4 节点清单里"
        print(
            f"{spec.name:<12} {'是' if spec.enabled else '否':<6} {state:<10} {restart:<6} "
            f"{spec.target_module}{flag}"
        )
    missing = [
        spec.name
        for spec in config.nodes
        if spec.enabled and not module_exists(spec.target_module, config.pythonpath)
    ]
    if config.unknown_keys:
        print()
        print(f"# ⚠ 清单里有 {len(config.unknown_keys)} 个未识别的键（已忽略）：")
        for key in config.unknown_keys:
            print(f"#   {key}")
        print("#   注意 TOML 里 `[env]` 是**顶层表**，节点级环境变量要写 `[node.env]`")
    if missing and config.skip_missing:
        print()
        print(
            f"# {len(missing)} 个节点尚未实现，将被标记 missing 并跳过：{', '.join(missing)}"
        )
        print("# 实现后无需改 toml —— run-all 会自动把它们带起来")
    return 0


def _find_node_class(module: Any, name: str, module_name: str) -> type[Node]:
    explicit = getattr(module, "NODE", None)
    if explicit is not None:
        if isinstance(explicit, type) and issubclass(explicit, Node):
            return explicit
        raise CliError(
            f"{module_name}.NODE 必须是 Node 子类（得到 {type(explicit).__name__}）；"
            "若要传实例请改用 run 之外的入口"
        )

    candidates: list[type[Node]] = []
    for value in vars(module).values():
        if (
            isinstance(value, type)
            and issubclass(value, Node)
            and value is not Node
            and not inspect.isabstract(value)
            and value not in candidates
        ):
            candidates.append(value)
    if not candidates:
        raise CliError(
            f"{module_name} 里没有可用的 Node 子类。\n"
            "  约定：模块需暴露一个实现了 setup() 的 Node 子类，或用 `NODE = XxxNode` 指明"
        )
    exact = [cls for cls in candidates if getattr(cls, "name", "") == name]
    if len(exact) == 1:
        return exact[0]
    if len(candidates) == 1:
        return candidates[0]
    names = ", ".join(f"{cls.__name__}(name={getattr(cls, 'name', '')!r})" for cls in candidates)
    raise CliError(f"{module_name} 里有多个 Node 子类，请用 `NODE = ...` 指明要用哪个：{names}")


def build_node(name: str, module: str | None, config: Config) -> Node:
    """按名字/模块导入并实例化一个节点。"""
    module_name = module or f"{DEFAULT_NODE_PACKAGE}.{name}"
    try:
        module_obj = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        missing = getattr(exc, "name", "") or ""
        if missing and (module_name == missing or module_name.startswith(f"{missing}.")):
            raise CliError(
                f"找不到节点模块 {module_name!r}\n"
                f"  节点实现放在 agent/nodes/{name}/（内含 __init__.py / __main__.py），\n"
                f"  或临时用 --module <模块名> 指定（测试/调试用）"
            ) from exc
        raise CliError(f"导入 {module_name!r} 时缺少依赖：{exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise CliError(f"导入节点模块 {module_name!r} 失败：{type(exc).__name__}: {exc}") from exc

    cls = _find_node_class(module_obj, name, module_name)
    try:
        return cls(config)
    except NodeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CliError(f"实例化 {module_name}.{cls.__name__} 失败：{type(exc).__name__}: {exc}") from exc


def _config_from_args(args: argparse.Namespace) -> Config:
    config = Config.load(getattr(args, "config", None))
    if getattr(args, "room", None):
        config.room = args.room
    if getattr(args, "log_level", None):
        config.log_level = normalize_level(args.log_level)
    if getattr(args, "log_dir", None):
        config.log_dir = Path(args.log_dir).resolve()
    return config


def cmd_run(args: argparse.Namespace) -> int:
    """单跑一个节点（不依赖 supervisor）。"""
    _apply_no_color(args)
    config = _config_from_args(args)
    node = build_node(args.name, args.module, config)
    return run_node(node)


def cmd_run_all(args: argparse.Namespace) -> int:
    """起 supervisor。"""
    _apply_no_color(args)
    config = load_config(args.config)
    if not config.enabled_nodes():
        print("# nodes.toml 里没有 enabled=true 的节点，无事可做", file=sys.stderr)
        return 0
    supervisor = Supervisor(config)
    return supervisor.run_forever()


def cmd_pub(args: argparse.Namespace) -> int:
    """手动发一条消息。payload 先按 JSON 解，解不开就当字符串发。"""
    config = _config_from_args(args)
    raw = args.payload
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        payload = raw
    key = topics.resolve(args.key, config.room)
    if args.dry_run:
        shown = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        print(f"# 将发布 → {key}：{shown}")
        return 0
    return run_node(_CliPubNode(config, args.key, encode_payload(payload)))


def cmd_sub(args: argparse.Namespace) -> int:
    """订阅并打印（Ctrl+C 退出）。"""
    config = _config_from_args(args)
    key = topics.resolve(args.key, config.room)
    return run_node(_CliSubNode(config, key))


# ──────────────────────────────────────────────────────────────
# 参数解析
# ──────────────────────────────────────────────────────────────
def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None, help="agent/config.toml 路径")
    parser.add_argument("--room", default=None, help="覆盖 room")
    parser.add_argument("--log-level", default=None, help="覆盖日志级别")
    parser.add_argument("--log-dir", default=None, help="覆盖日志目录")
    parser.add_argument("--no-color", action="store_true", help="终端日志不上色（等同 set COYOTELINK_COLOR=0）")


def _apply_no_color(args: argparse.Namespace) -> None:
    """`--no-color` → 关掉终端着色（在 `Config.load` 之前调用，越早越稳）。"""
    if getattr(args, "no_color", False):
        os.environ[logging_mod.COLOR_ENV] = "0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent.core.cli",
        description="CoyoteLink 节点框架命令行（TF）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m agent.core.cli nodes\n"
            "  python -m agent.core.cli run pose\n"
            "  python -m agent.core.cli run-all\n"
            f"  python -m agent.core.cli sub '{topics.test_keys()}'\n"
            f"  python -m agent.core.cli pub {topics.FEATURE_POSE} '{{\"ts\": 1}}'\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    p = subparsers.add_parser("doctor", help="环境自检（复用 tools/doctor.py）")
    p.set_defaults(func=cmd_doctor)

    p = subparsers.add_parser("topics", help="打印 key space（唯一真源的当前内容）")
    p.add_argument("--room", default=None)
    p.set_defaults(func=cmd_topics)

    p = subparsers.add_parser("nodes", help="列出 nodes.toml 的节点与状态")
    p.add_argument("--config", default=None, help="nodes.toml 路径")
    p.set_defaults(func=cmd_nodes)

    p = subparsers.add_parser("run", help="单跑一个节点（调试用）")
    p.add_argument("name", help="节点名，例如 pose")
    p.add_argument("--module", default=None, help="覆盖模块名（默认 agent.nodes.<name>）")
    _add_common(p)
    p.set_defaults(func=cmd_run)

    p = subparsers.add_parser("run-all", help="起 supervisor（读 nodes.toml）")
    p.add_argument("--config", default=None, help="nodes.toml 路径")
    p.add_argument("--no-color", action="store_true", help="终端日志不上色")
    p.set_defaults(func=cmd_run_all)

    p = subparsers.add_parser("pub", help="发一条消息")
    p.add_argument("key", help="完整 key 或后缀（如 feature/pose）")
    p.add_argument("payload", nargs="?", default="{}", help="JSON 或字符串")
    p.add_argument("--dry-run", action="store_true", help="只打印解析结果，不真发")
    _add_common(p)
    p.set_defaults(func=cmd_pub)

    p = subparsers.add_parser("sub", help="订阅并打印")
    p.add_argument("key", help="完整 key 或后缀，支持 ** 和 *")
    _add_common(p)
    p.set_defaults(func=cmd_sub)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口（同步，返回退出码）。"""
    force_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n# 已中断", file=sys.stderr)
        return 130
    except (CliError, ConfigError, SupervisorError, NodeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI 顶层必须外显
        print(f"未预期的错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
