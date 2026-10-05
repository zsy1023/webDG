"""进程管理 + 自动重启（supervisor）。

来源：`docs/tasks/TF-节点框架与消息总线.md` §4.6 + 框架文档 §5.5。

行为契约：

1. 读 `agent/nodes.toml`，为每个 `enabled=true` 的节点起子进程
   （默认 `python -m agent.nodes.<name>`，可用 `module` 覆盖）
2. 非正常退出 → 按**指数退避**重启（有上限，避免重启风暴）
3. **supervisor 自身崩溃 → 已启动的子进程继续运行**（子进程不依赖它存活）
4. `Ctrl+C` → 优雅停止全部子进程
5. `skip_missing = true` → 模块不存在的节点标记 `missing` 并跳过（不进入重启循环）
6. 停子进程**三级兜底**：`CTRL_BREAK`（先试优雅）→ 等 `stop_timeout_ms`
   → `terminate()` → 再等 → `kill()`

为什么必须这么做（实测结论）：Windows 上 `Popen.terminate()` 就是 `TerminateProcess`，
Python 子进程**捕获不到任何信号**；唯一能被 Python 捕获的优雅停止是
`CTRL_BREAK_EVENT`（要求子进程用 `CREATE_NEW_PROCESS_GROUP` 启动）→ `SIGBREAK`。
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import signal
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import topics
from .config import default_project_root
from .logging import setup_logging, style

__all__ = [
    "SupervisorError",
    "NodeSpec",
    "SupervisorConfig",
    "ManagedNode",
    "Supervisor",
    "load_config",
    "STATE_DISABLED",
    "STATE_MISSING",
    "STATE_PENDING",
    "STATE_RUNNING",
    "STATE_RESTARTING",
    "STATE_STOPPED",
    "STATE_FAILED",
]

#: 节点默认所在包（`module` 未指定时用 `agent.nodes.<name>`）
DEFAULT_NODE_PACKAGE = "agent.nodes"

STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_RESTARTING = "restarting"
STATE_STOPPED = "stopped"
STATE_FAILED = "failed"
STATE_MISSING = "missing"
STATE_DISABLED = "disabled"


class SupervisorError(RuntimeError):
    """节点清单有问题（缺文件 / 语法错 / 字段类型错）。"""


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except (ValueError, AttributeError):  # pragma: no cover
        return f"signal {signum}"


# ──────────────────────────────────────────────────────────────
# 清单模型
# ──────────────────────────────────────────────────────────────
@dataclass
class NodeSpec:
    """`[[node]]` 一项。"""

    name: str
    enabled: bool = True
    module: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    restart: bool | None = None  # None = 跟随 [supervisor].restart

    @property
    def target_module(self) -> str:
        return self.module or f"{DEFAULT_NODE_PACKAGE}.{self.name}"


@dataclass
class SupervisorConfig:
    """`[supervisor]` 一节 + 节点列表。"""

    nodes: list[NodeSpec] = field(default_factory=list)
    restart: bool = True
    backoff_initial_ms: int = 500
    backoff_max_ms: int = 30000
    max_restarts: int = 10
    stop_timeout_ms: int = 5000
    skip_missing: bool = True
    backoff_reset_after_s: float = 60.0
    restart_on_clean_exit: bool = False
    pythonpath: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    source: Path | None = None
    #: 清单里没被识别的键（例如把 `[node.env]` 误写成 `[env]`）——`cli nodes` 会提示。
    unknown_keys: list[str] = field(default_factory=list)

    def enabled_nodes(self) -> list[NodeSpec]:
        return [spec for spec in self.nodes if spec.enabled]


def _require_bool(section: dict[str, Any], key: str, default: bool, where: str) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise SupervisorError(f"{where}：{key} 必须是布尔值（true / false），得到 {value!r}")
    return value


def _require_int(section: dict[str, Any], key: str, default: int, where: str) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SupervisorError(f"{where}：{key} 必须是整数，得到 {value!r}")
    return value


def _require_number(section: dict[str, Any], key: str, default: float, where: str) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SupervisorError(f"{where}：{key} 必须是数字，得到 {value!r}")
    return float(value)


def _require_str_list(section: dict[str, Any], key: str, where: str) -> list[str]:
    value = section.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SupervisorError(f"{where}：{key} 必须是字符串数组，得到 {value!r}")
    return list(value)


def _require_str_map(section: dict[str, Any], key: str, where: str) -> dict[str, str]:
    value = section.get(key, {})
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise SupervisorError(f"{where}：{key} 必须是字符串到字符串的表，得到 {value!r}")
    return {str(k): str(v) for k, v in value.items()}


def load_config(
    path: str | Path | None = None, *, project_root: str | Path | None = None
) -> SupervisorConfig:
    """读节点清单。出错一律抛 `SupervisorError`，信息里带上**文件路径**和**原因**。

    `path=None` → `<project_root>/agent/nodes.toml`；显式路径不存在则报错。
    """
    root = Path(project_root).resolve() if project_root else default_project_root()
    explicit = path is not None
    cfg_path = Path(path) if explicit else root / "agent" / "nodes.toml"
    if not cfg_path.is_absolute():
        cfg_path = (root / cfg_path).resolve()

    if not cfg_path.exists():
        hint = (
            "显式指定的路径必须存在。"
            if explicit
            else f"请在 {root / 'agent'} 下创建 nodes.toml（可参考任务书 §4.6 的样例）。"
        )
        raise SupervisorError(f"节点清单不存在：{cfg_path}\n  {hint}")
    if not cfg_path.is_file():
        raise SupervisorError(f"节点清单不是文件：{cfg_path}")

    try:
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise SupervisorError(f"节点清单 TOML 语法错误：{cfg_path}\n  {exc}") from exc
    except OSError as exc:
        raise SupervisorError(f"节点清单读取失败：{cfg_path}\n  {exc}") from exc

    if not isinstance(data, dict):
        raise SupervisorError(f"节点清单顶层必须是表（table）：{cfg_path}")

    section = data.get("supervisor") or {}
    if not isinstance(section, dict):
        raise SupervisorError(f"[supervisor] 必须是表（table）：{cfg_path}")
    where = f"[supervisor]（{cfg_path}）"

    # 未识别的键只提示不报错（向前兼容），但必须**看得见** ——
    # 典型的坑是把 `[node.env]` 写成 `[env]`（TOML 里后者是**顶层**表），
    # 结果配置被静默忽略。
    unknown_keys: list[str] = []

    nodes_raw = data.get("node", [])
    if not isinstance(nodes_raw, list):
        raise SupervisorError(f"[[node]] 必须是数组表（array of tables）：{cfg_path}")

    specs: list[NodeSpec] = []
    seen: set[str] = set()
    for index, entry in enumerate(nodes_raw, start=1):
        node_where = f"[[node]] 第 {index} 项（{cfg_path}）"
        if not isinstance(entry, dict):
            raise SupervisorError(f"{node_where} 必须是表（table），得到 {type(entry).__name__}")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise SupervisorError(f"{node_where} 缺少 name（必须是非空字符串）")
        name = name.strip()
        if name in seen:
            raise SupervisorError(f"{node_where} 节点名重复：{name!r}（name 必须唯一）")
        seen.add(name)

        unknown_keys.extend(
            f"node[{name}].{key}"
            for key in sorted(set(entry) - {"name", "enabled", "module", "args", "env", "restart"})
        )

        module = entry.get("module")
        if module is not None and (not isinstance(module, str) or not module.strip()):
            raise SupervisorError(f"{node_where} 的 module 必须是非空字符串")
        restart = entry.get("restart")
        if restart is not None and not isinstance(restart, bool):
            raise SupervisorError(f"{node_where} 的 restart 必须是布尔值，得到 {restart!r}")

        specs.append(
            NodeSpec(
                name=name,
                enabled=_require_bool(entry, "enabled", True, node_where),
                module=module.strip() if isinstance(module, str) else None,
                args=tuple(_require_str_list(entry, "args", node_where)),
                env=_require_str_map(entry, "env", node_where),
                restart=restart,
            )
        )

    known_top = {"supervisor", "node"}
    unknown_keys[:0] = sorted(set(data) - known_top)
    known_section = {
        "restart",
        "backoff_initial_ms",
        "backoff_max_ms",
        "max_restarts",
        "stop_timeout_ms",
        "skip_missing",
        "backoff_reset_after_s",
        "restart_on_clean_exit",
        "pythonpath",
        "env",
    }
    unknown_keys[:0] = [f"supervisor.{k}" for k in sorted(set(section) - known_section)]

    stop_timeout = _require_int(section, "stop_timeout_ms", 5000, where)
    if stop_timeout < 0:
        raise SupervisorError(f"{where}：stop_timeout_ms 不能为负，得到 {stop_timeout}")
    max_restarts = _require_int(section, "max_restarts", 10, where)
    if max_restarts < 0:
        raise SupervisorError(f"{where}：max_restarts 不能为负，得到 {max_restarts}")
    backoff_initial = _require_int(section, "backoff_initial_ms", 500, where)
    backoff_max = _require_int(section, "backoff_max_ms", 30000, where)
    if backoff_initial <= 0 or backoff_max <= 0:
        raise SupervisorError(f"{where}：退避时间必须为正（得到 {backoff_initial} / {backoff_max}）")
    if backoff_max < backoff_initial:
        raise SupervisorError(
            f"{where}：backoff_max_ms({backoff_max}) 不能小于 backoff_initial_ms({backoff_initial})"
        )

    return SupervisorConfig(
        nodes=specs,
        restart=_require_bool(section, "restart", True, where),
        backoff_initial_ms=backoff_initial,
        backoff_max_ms=backoff_max,
        max_restarts=max_restarts,
        stop_timeout_ms=stop_timeout,
        skip_missing=_require_bool(section, "skip_missing", True, where),
        backoff_reset_after_s=_require_number(section, "backoff_reset_after_s", 60.0, where),
        restart_on_clean_exit=_require_bool(section, "restart_on_clean_exit", False, where),
        pythonpath=_require_str_list(section, "pythonpath", where),
        env=_require_str_map(section, "env", where),
        source=cfg_path,
        unknown_keys=unknown_keys,
    )


def module_exists(module: str, extra_paths: Iterable[str] = ()) -> bool:
    """模块能否被导入**且确实是一个节点**（用于 `skip_missing`）。

    ⚠️ **为什么不能只看 `find_spec`**：PEP 420 命名空间包**只要目录存在**就能被 find_spec 找到。
    `agent/nodes/sim_coyote/` 曾只放了一个探针脚本（既没有 `__init__.py` 也没有 `node.py`），
    于是被误判成"就绪"，supervisor 会去启动它、然后失败。

    ✅ 判据：**目录包必须带 `__init__.py`**（正式包）才算实现了节点。
    - 只有目录（命名空间包）→ 还没实现 → `missing`
    - 带 `__init__.py` → 已实现（节点类可以放 `__init__.py`，如 `coyote_v4`；也可以放 `node.py`）

    `extra_paths` 是清单里 `[supervisor].pythonpath` 的内容：**子进程**会拿到它，
    但 supervisor 自己检查"模块是否存在"时如果只看自己的 `sys.path`，就会把
    明明能启动的节点误判成 missing。所以这里临时把那些路径也纳入查找，
    查完立刻还原（含 `invalidate_caches()`，不留 import 缓存副作用）。
    """
    added = [str(p) for p in extra_paths if p and str(p) not in sys.path]
    sys.path[:0] = added
    try:
        spec = importlib.util.find_spec(module)
        if spec is None:
            return False
        # 目录包 / 命名空间包 → 必须带 __init__.py（正式包）才算"实现了节点"
        locations = list(spec.submodule_search_locations or ())
        if locations:
            return any(
                os.path.isfile(os.path.join(loc, "__init__.py")) for loc in locations
            )
        # 单文件模块（如 agent/nodes/foo.py）也算
        return spec.origin is not None and spec.origin.endswith(".py")
    except (ImportError, ValueError, ModuleNotFoundError, AttributeError):
        return False
    finally:
        if added:
            for path in added:
                with contextlib.suppress(ValueError):
                    sys.path.remove(path)
            importlib.invalidate_caches()


# ──────────────────────────────────────────────────────────────
# 运行时
# ──────────────────────────────────────────────────────────────
@dataclass
class ManagedNode:
    """一个被托管节点的运行时状态。"""

    spec: NodeSpec
    state: str = STATE_PENDING
    pid: int | None = None
    restarts: int = 0
    last_exit_code: int | None = None
    last_start_ts: float | None = None
    next_start_ts: float = 0.0
    proc: subprocess.Popen[bytes] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.spec.name,
            "enabled": self.spec.enabled,
            "module": self.spec.target_module,
            "state": self.state,
            "pid": self.pid,
            "restarts": self.restarts,
            "last_exit_code": self.last_exit_code,
            "error": self.error,
            "known_node": self.spec.name in topics.NODE_NAMES,
        }


class Supervisor:
    """读清单 → 起子进程 → 监控 → 退避重启 → 优雅停止。"""

    def __init__(
        self,
        config: SupervisorConfig,
        *,
        project_root: str | Path | None = None,
        log: Any = None,
        stdout: Any = None,
        stderr: Any = None,
    ) -> None:
        self.config = config
        self.project_root = (
            Path(project_root).resolve() if project_root else default_project_root()
        )
        self.log = log or setup_logging("supervisor")
        self._stdout = stdout
        self._stderr = stderr
        self._stopping = False
        self._children: list[ManagedNode] = [
            ManagedNode(spec=spec) for spec in config.nodes
        ]

    # —— 只读视图 ——
    @property
    def nodes(self) -> list[ManagedNode]:
        return self._children

    def get(self, name: str) -> ManagedNode:
        for node in self._children:
            if node.spec.name == name:
                return node
        raise KeyError(name)

    def status(self) -> list[dict[str, Any]]:
        return [node.to_dict() for node in self._children]

    @property
    def alive(self) -> list[ManagedNode]:
        return [
            node
            for node in self._children
            if node.proc is not None and node.proc.poll() is None
        ]

    # —— 子进程环境 ——
    def _child_env(self, spec: NodeSpec) -> dict[str, str]:
        env = dict(os.environ)
        path_parts = [str(self.project_root)]
        path_parts.extend(self.config.pythonpath)
        existing = env.get("PYTHONPATH")
        if existing:
            path_parts.extend(part for part in existing.split(os.pathsep) if part)
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(path_parts))
        env["COYOTELINK_NODE"] = spec.name
        env["COYOTELINK_ROOT"] = str(self.project_root)
        env.update(self.config.env)
        env.update(spec.env)
        return env

    def _popen_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "cwd": str(self.project_root),
            "stdout": self._stdout,
            "stderr": self._stderr,
        }
        if os.name == "nt":
            # ★ 关键：独立进程组 → 可以用 CTRL_BREAK_EVENT 优雅停止；
            #   也顺带让 supervisor 自己的 Ctrl+C 不会误伤子进程
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        return kwargs

    def start(self) -> None:
        """为所有 `enabled=true` 的节点起子进程（`skip_missing` 生效）。"""
        self._stopping = False
        self._log_banner()
        for node in self._children:
            spec = node.spec
            if not spec.enabled:
                node.state = STATE_DISABLED
                continue
            if self.config.skip_missing and not module_exists(
                spec.target_module, self.config.pythonpath
            ):
                node.state = STATE_MISSING
                node.error = f"模块不存在：{spec.target_module}"
                self.log.warning(
                    "节点 %s 标记 missing（%s），跳过；实现后无需改 toml 即会自动生效",
                    spec.name,
                    node.error,
                )
                continue
            self._spawn(node)

    def _log_banner(self) -> None:
        """起子进程之前先打一张"要起什么、怎么用"的彩色清单。

        为什么值得单独打：`run-all` 一上来就 12 个进程混着刷日志，用户根本看不出
        "面板地址是哪个、房间是什么、哪个节点会缺席"。这三件事在这里一次说清。
        """
        names = [n.spec.name for n in self._children if n.spec.enabled]
        self.log.info(
            "启动 %d 个节点：%s",
            len(names),
            style(" ".join(names), "bold_cyan"),
        )
        # 房间/日志目录只有 `Config` 知道（supervisor 自己读的是 nodes.toml）——
        # 读不到就算了，不能让一条横幅拦住启动。
        # 注意：`Config.load` 是**延迟导入**的，免得 supervisor 与 Config 循环引用。
        try:
            from .config import Config

            effective = Config.load()
        except Exception:  # noqa: BLE001
            effective = None
        if effective is not None:
            self.log.info(
                "房间 room=%s（面板上创建/加入房间会改它）", style(effective.room, "magenta")
            )
        for node in self._children:
            if node.spec.name != "web" or not node.spec.enabled:
                continue
            args = list(node.spec.args)
            port = 8780
            if "--port" in args:
                index = args.index("--port")
                if index + 1 < len(args):
                    port = args[index + 1]
            self.log.info(
                "控制面板：%s（浏览器打开；Ctrl+C 停全部节点）",
                style(f"http://127.0.0.1:{port}", "bold_green"),
            )
            break
        if effective is not None:
            self.log.info("日志落盘：%s", style(str(effective.log_dir), "gray"))

    def _spawn(self, node: ManagedNode) -> None:
        spec = node.spec
        cmd = [sys.executable, "-m", spec.target_module, *spec.args]
        try:
            proc = subprocess.Popen(cmd, env=self._child_env(spec), **self._popen_kwargs())
        except OSError as exc:
            node.state = STATE_FAILED
            node.error = f"启动失败：{exc}"
            self.log.error("节点 %s 启动失败：%s", spec.name, exc)
            return
        node.proc = proc
        node.pid = proc.pid
        node.state = STATE_RUNNING
        node.last_start_ts = time.monotonic()
        node.next_start_ts = 0.0
        node.error = None
        self.log.info("节点 %s 已启动（pid=%d，module=%s）", spec.name, proc.pid, spec.target_module)

    # —— 巡检 ——
    def poll(self) -> None:
        """单次巡检：回收退出的子进程 + 到期重启。不阻塞。"""
        if self._stopping:
            return
        now = time.monotonic()
        for node in self._children:
            if node.proc is not None:
                if self._reap(node):
                    self._handle_exit(node, now)
                continue
            if node.state == STATE_RESTARTING and now >= node.next_start_ts:
                self._spawn(node)

    def _reap(self, node: ManagedNode) -> bool:
        proc = node.proc
        if proc is None:
            return True
        code = proc.poll()
        if code is None:
            return False
        node.last_exit_code = code
        node.proc = None
        node.pid = None
        return True

    def backoff_delay_ms(self, attempt: int) -> float:
        """第 `attempt` 次重启前等多久（指数退避，封顶）。"""
        attempt = max(1, int(attempt))
        return float(min(self.config.backoff_initial_ms * (2 ** (attempt - 1)), self.config.backoff_max_ms))

    def _should_restart(self, node: ManagedNode, code: int | None) -> bool:
        restart = self.config.restart if node.spec.restart is None else node.spec.restart
        if not restart:
            return False
        if code == 0 and not self.config.restart_on_clean_exit:
            return False
        return True

    def _handle_exit(self, node: ManagedNode, now: float) -> None:
        spec = node.spec
        code = node.last_exit_code
        lifetime = now - (node.last_start_ts or now)

        # 稳定运行过一段时间 → 退避计数清零（免得"一天崩一次"最终被判 failed）
        if node.restarts and lifetime >= self.config.backoff_reset_after_s:
            self.log.info(
                "节点 %s 已稳定运行 %.0fs，重置重启计数（原 %d 次）",
                spec.name,
                lifetime,
                node.restarts,
            )
            node.restarts = 0

        if not self._should_restart(node, code):
            node.state = STATE_STOPPED if code == 0 else STATE_FAILED
            node.error = None if code == 0 else f"退出码 {code}"
            self.log.warning(
                "节点 %s 退出（code=%s，运行 %.1fs），按配置不重启", spec.name, code, lifetime
            )
            return

        # 超过上限就彻底停手（防重启风暴）。注意：restarts 只在**真的重启**时 +1，
        # 所以最终 `restarts == max_restarts`，语义是"一共重启了几次"。
        if node.restarts >= self.config.max_restarts:
            node.state = STATE_FAILED
            node.error = (
                f"已重启 {node.restarts} 次仍失败（上限 {self.config.max_restarts}），不再重启"
            )
            self.log.error("节点 %s 标记 failed：%s", spec.name, node.error)
            return

        node.restarts += 1
        delay_ms = self.backoff_delay_ms(node.restarts)
        node.state = STATE_RESTARTING
        node.next_start_ts = now + delay_ms / 1000.0
        node.error = f"退出码 {code}"
        self.log.warning(
            "节点 %s 异常退出（code=%s，运行 %.1fs）→ %.0fms 后进行第 %d/%d 次重启",
            spec.name,
            code,
            lifetime,
            delay_ms,
            node.restarts,
            self.config.max_restarts,
        )

    # —— 停止 ——
    def _signal_graceful(self, node: ManagedNode) -> None:
        proc = node.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()  # POSIX: SIGTERM，Python 能捕获
        except (OSError, ValueError, AttributeError) as exc:
            self.log.warning("节点 %s 优雅停止信号发送失败（%s），退回 terminate()", node.spec.name, exc)
            with contextlib.suppress(Exception):
                proc.terminate()

    def _wait_exit(self, nodes: list[ManagedNode], timeout: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if all(node.proc is None or node.proc.poll() is not None for node in nodes):
                return
            time.sleep(0.05)

    def stop_all(self, *, timeout: float | None = None) -> None:
        """优雅停止全部子进程（三级兜底）。"""
        self._stopping = True
        pending = [node for node in self._children if node.proc is not None and node.proc.poll() is None]
        if not pending:
            self._finalize_stop()
            return

        grace = self.config.stop_timeout_ms / 1000.0 if timeout is None else float(timeout)
        self.log.info("停止 %d 个子进程（先优雅信号，等 %.1fs）", len(pending), grace)
        for node in pending:
            self._signal_graceful(node)
        self._wait_exit(pending, grace)

        left = [node for node in pending if node.proc is not None and node.proc.poll() is None]
        if left:
            self.log.warning("%d 个子进程未在 %.1fs 内退出，改用 terminate()", len(left), grace)
            for node in left:
                with contextlib.suppress(Exception):
                    assert node.proc is not None
                    node.proc.terminate()
            self._wait_exit(left, 2.0)

        left = [node for node in left if node.proc is not None and node.proc.poll() is None]
        if left:
            self.log.error("%d 个子进程仍未退出，kill() 强杀", len(left))
            for node in left:
                with contextlib.suppress(Exception):
                    assert node.proc is not None
                    node.proc.kill()
            self._wait_exit(left, 3.0)

        self._finalize_stop()

    def _finalize_stop(self) -> None:
        for node in self._children:
            if node.proc is not None and node.proc.poll() is None:
                continue
            self._reap(node)
            if node.state in (STATE_RUNNING, STATE_RESTARTING, STATE_PENDING):
                node.state = STATE_STOPPED
                node.error = None
        self.log.info("全部子进程已停止")

    def _force_kill_all(self) -> None:
        for node in self._children:
            if node.proc is not None and node.proc.poll() is None:
                with contextlib.suppress(Exception):
                    node.proc.kill()

    # —— 主循环 ——
    def serve(self, stop_event: threading.Event | None = None, *, interval: float = 0.2) -> int:
        """启动 + 巡检循环，直到 `stop_event` 被设置。返回退出码 0。"""
        stop = stop_event if stop_event is not None else threading.Event()
        self.start()
        try:
            while not stop.is_set():
                self.poll()
                stop.wait(interval)
        finally:
            self.stop_all()
        self.log.info("supervisor 已退出（子进程已全部停止）")
        return 0

    def run_forever(self, *, interval: float = 0.2) -> int:
        """`serve()` 的信号版：Ctrl+C → 优雅停止。"""
        stop = threading.Event()
        previous: dict[int, Any] = {}

        def _handler(signum: int, _frame: Any) -> None:
            if stop.is_set():
                self.log.warning("再次收到 %s，立即强杀子进程", _signal_name(signum))
                self._force_kill_all()
                return
            self.log.info("收到 %s，停止全部节点", _signal_name(signum))
            stop.set()

        if threading.current_thread() is threading.main_thread():
            for signame in ("SIGINT", "SIGTERM", "SIGBREAK"):
                signum = getattr(signal, signame, None)
                if signum is None:
                    continue
                try:
                    previous[signum] = signal.signal(signum, _handler)
                except (ValueError, OSError, RuntimeError):
                    continue

        try:
            return self.serve(stop, interval=interval)
        finally:
            for signum, old in previous.items():
                with contextlib.suppress(Exception):
                    signal.signal(signum, old)
