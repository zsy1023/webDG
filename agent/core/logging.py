"""统一日志（UTF-8 强制 + **终端彩色**）。

来源：`docs/tasks/TF-节点框架与消息总线.md` §4.5。

格式：``2026-01-01 12:00:00 [pose] INFO  消息``

要点：

- **所有 FileHandler 显式 `encoding="utf-8"`** —— 否则含 `®` / 中文的日志会炸；
- 控制台 handler 也走 UTF-8（通过 `config.force_utf8()` 重设 stdout/stderr）；
- logger 名字 = 节点名，因此每条日志自带 `[节点名]` 前缀；
- `propagate = False`，避免与 root logger 重复输出。

终端彩色（2026-10 加）
----------------------

**落盘永远是纯文本**，只有控制台会带 ANSI 颜色（"终端给人看、文件给机器看"）：

- 级别配色：DEBUG 暗灰 / INFO 青 / WARNING 黄 / ERROR 红 / CRITICAL 红底；
- 节点名配色：按名字散列到一个调色板 —— 一眼能分清是哪个节点在说话；
- 消息关键词高亮：开火/惩罚（金）、房间（品红）、连接就绪（绿）、
  失败/超时/拒绝（红）、丢弃/忽略（暗）、key 名（青）。
  一条消息里**多处**命中也能各上各的色（单遍正则，不会互相嵌套）。

开关（谁先命中听谁的）：

1. `NO_COLOR` 环境变量存在 → 关（<https://no-color.org> 的通用约定）；
2. `COYOTELINK_COLOR=0/1` → 显式开关（CLI 的 `--no-color` 就是设它）；
3. 都没设 → **看流是不是 TTY**：终端里彩色，重定向/管道/pytest 捕获里纯文本。

Windows 上还要先打开 VT 处理（`ENABLE_VIRTUAL_TERMINAL_PROCESSING`），
否则 `\\x1b[31m` 会以 `←[31m` 这种乱码原样打出来 —— 这一步用 ctypes 做，不引入新依赖。

⚠️ 本文件与标准库 `logging` **同名**。Python 3 是绝对导入，模块内 `import logging`
拿到的是标准库（不是自己），全项目也一律用 `python -m agent.core.*` 启动，
所以不存在遮蔽问题。
"""

from __future__ import annotations

import io
import logging  # 标准库
import os
import re
import sys
from pathlib import Path
from typing import Any

from . import topics
from .config import force_utf8, normalize_level

LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)-5s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_MARK = "_coyotelink_handler"

# ──────────────────────────────────────────────────────────────
# 颜色
# ──────────────────────────────────────────────────────────────
#: 环境变量：`0/false/no` 关、`1/true/yes` 开（都没设就看 TTY）
COLOR_ENV = "COYOTELINK_COLOR"

_RESET = "\x1b[0m"
_CODES: dict[str, str] = {
    "reset": _RESET,
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "blue": "\x1b[34m",
    "magenta": "\x1b[35m",
    "cyan": "\x1b[36m",
    "gray": "\x1b[90m",
    "gold": "\x1b[38;5;220m",     # 郊狼黑金的"金"，跟面板同色系
    "bold_red": "\x1b[1;31m",
    "bold_green": "\x1b[1;32m",
    "bold_yellow": "\x1b[1;33m",
    "bold_cyan": "\x1b[1;36m",
    "bold_gold": "\x1b[1;38;5;220m",
    "white_on_red": "\x1b[1;37;41m",
}

#: 级别 → 颜色
LEVEL_STYLES: dict[str, str] = {
    "DEBUG": "gray",
    "INFO": "cyan",
    "WARNING": "yellow",
    "ERROR": "red",
    "CRITICAL": "white_on_red",
}

#: 节点名 → 颜色（按名字散列，稳定；同一个节点每次都是同一个色）
_NODE_STYLES: tuple[str, ...] = (
    "bold_cyan", "bold_green", "bold_yellow", "magenta", "blue", "bold_gold", "green",
)

#: 消息里 key 名的识别式 —— **前缀和后缀都从 `topics` 推**，不在这儿手抄
#: （`tests/test_topics.py` 有一条 AST 扫描专门守"key 知识只在 topics.py"）。
_KEY_NAMESPACES: tuple[str, ...] = tuple(
    sorted({suffix.split("/", 1)[0] for suffix in topics.ALL_SUFFIXES})
)
_KEY_PATTERN = "|".join(
    [
        re.escape(f"{topics.PREFIX}/") + r"[\w*/\-.]*",
        r"(?<![\w/])(?:"
        + "|".join(re.escape(ns) for ns in _KEY_NAMESPACES)
        + r")/[\w*/\-.]*",
    ]
)

#: 消息关键词高亮：**顺序即优先级**（同一起点先匹配到的赢）。
#: 一整个 alternation 单遍替换 —— 多处命中各上各的色，且不会嵌套。
_MESSAGE_RULES: tuple[tuple[str, str], ...] = (
    # 先放"坏消息"和"高危动作"，免得被后面的"连接""房间"等词先吃掉
    (r"失败|错误|异常|拒绝|超时|崩(?!溃)|无响应|解密失败|不行", "red"),
    (r"未加密|人人可推|容易(?:被)?猜|不安全|警告|告警", "yellow"),
    (r"开火|已开火|惩罚|清场|急停|OFF|停火", "bold_gold"),
    (r"房间|换房", "magenta"),
    (r"已连接|已打开|就绪|已重连|通过|PASS|注册成功|已启动", "green"),
    (r"丢弃|忽略|跳过|降级", "dim"),
    (_KEY_PATTERN, "cyan"),                      # 完整 key 或后缀
    (r"\[T\d+\]|\(T\d+\)", "dim"),
)
_MESSAGE_RE = re.compile("|".join(f"(?P<r{i}>{p})" for i, (p, _) in enumerate(_MESSAGE_RULES)))
_MESSAGE_STYLE_BY_GROUP = {
    f"r{i}": style for i, (_, style) in enumerate(_MESSAGE_RULES)
}


def enable_windows_ansi(stream: Any = None) -> bool:
    """在 Windows 控制台上打开 ANSI/VT 处理。非 Windows 或失败 → False（调用方降级）。"""
    if os.name != "nt":
        return False
    try:
        import ctypes

        handle = None
        fileno = getattr(stream, "fileno", None)
        if fileno is not None:
            try:
                handle = ctypes.windll.kernel32.GetStdHandle(-11 if stream is sys.stdout else -12)
            except Exception:  # noqa: BLE001
                handle = None
        if not handle:
            handle = ctypes.windll.kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # 0x0004 = ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # noqa: BLE001 - 拿不到控制台（重定向 / 无 console）就纯文本
        return False


def color_enabled(stream: Any = None) -> bool:
    """这个流该不该上色（见模块头部的开关优先级）。"""
    if os.environ.get("NO_COLOR"):
        return False
    flag = str(os.environ.get(COLOR_ENV, "")).strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on"):
        return True
    target = stream if stream is not None else sys.stdout
    isatty = getattr(target, "isatty", None)
    if isatty is None or not isatty():
        return False
    return enable_windows_ansi(target) if os.name == "nt" else True


def style(text: Any, *styles: str) -> str:
    """给一段文本上色（没开颜色时原样返回）。`styles` 可叠加：`style(x, "bold", "red")`。"""
    body = str(text)
    codes = "".join(_CODES[s] for s in styles if s in _CODES)
    return f"{codes}{body}{_RESET}" if codes else body


def _node_style(name: str) -> str:
    if not name:
        return "reset"
    return _NODE_STYLES[sum(ord(c) for c in name) % len(_NODE_STYLES)]


def _highlight(message: str) -> str:
    """消息里的关键词着色（单遍正则；未命中就原样返回）。"""
    return _MESSAGE_RE.sub(
        lambda m: style(m.group(), _MESSAGE_STYLE_BY_GROUP[m.lastgroup or ""]),
        message,
    )


class ColorFormatter(logging.Formatter):
    """终端用：级别、节点名、消息关键词都上色。**只给控制台用，别给文件用。**"""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003 - 覆盖标准库签名
        stamp = self.formatTime(record, self.datefmt)
        message = record.getMessage()
        line = (
            f"{style(stamp, 'gray')} "
            f"[{style(record.name, _node_style(record.name))}] "
            f"{style(f'{record.levelname:<5}', LEVEL_STYLES.get(record.levelname, 'reset'))} "
            f"{_highlight(message)}"
        )
        if record.exc_info:
            line = f"{line}\n{style(self.formatException(record.exc_info), 'red')}"
        if record.stack_info:
            line = f"{line}\n{style(record.stack_info, 'dim')}"
        return line


def _console_stream(stream: Any = None) -> Any:
    """返回一个尽量 UTF-8 的控制台流。

    首选直接重设（`force_utf8()`），因为它在 pytest capture、真实控制台、
    重定向三种情况下都安全。流不支持 `reconfigure` 时不强行包装底层 buffer
    —— 那会导致解释器退出时重复关闭同一 fd。
    """
    if stream is None:
        force_utf8()
        return sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, TypeError, AttributeError):
            pass
    return stream


def setup_logging(
    name: str,
    *,
    level: str | int = "INFO",
    log_dir: str | Path | None = None,
    console: bool = True,
    log_file: bool = True,
    stream: Any = None,
    color: bool | None = None,
) -> logging.Logger:
    """配置并返回一个带节点名前缀的 logger。

    幂等：重复调用会**替换**自己加过的 handler（不会叠加出重复日志行）。
    `log_dir=None` 或 `log_file=False` 时不落盘（CLI 一次性命令用）。
    `stream` 指定控制台输出目标（CLI 用 stderr，把 stdout 留给管道数据）。
    `color=None` 时按 `color_enabled()` 自动判断；**文件 handler 永远是纯文本**。
    """
    logger = logging.getLogger(name)
    logger.setLevel(normalize_level(level))

    # 只清理"我们自己加的" handler，别动宿主（例如 pytest）装的
    for handler in [h for h in logger.handlers if getattr(h, _MARK, False)]:
        logger.removeHandler(handler)
        handler.close()

    if console:
        target = _console_stream(stream)
        stream_handler = logging.StreamHandler(target)
        want_color = color_enabled(target) if color is None else bool(color)
        if want_color:
            stream_handler.setFormatter(ColorFormatter(LOG_FORMAT, DATE_FORMAT))
        else:
            stream_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
        setattr(stream_handler, _MARK, True)
        logger.addHandler(stream_handler)

    if log_file and log_dir is not None:
        directory = Path(log_dir)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(
                directory / f"{name}.log",
                mode="a",
                encoding="utf-8",  # ★ 显式 UTF-8，任务书 §4.5 硬性要求
                delay=False,
            )
        except OSError as exc:  # 日志目录不可写不能拖垮节点，但必须可见
            logger.warning("日志文件无法创建（%s）：%s", directory / f"{name}.log", exc)
        else:
            # ★ 落盘一律纯文本：ANSI 转义码写进文件，grep/less 里全是乱码
            file_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
            setattr(file_handler, _MARK, True)
            logger.addHandler(file_handler)

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


def get_logger(name: str) -> logging.Logger:
    """取一个已经配置好的 logger（没配置过就用默认级别建一个）。"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        return setup_logging(name)
    return logger


def setup_root_logging(*, level: str | int = "INFO", color: bool | None = None) -> logging.Logger:
    """给**根 logger** 配一个（彩色）控制台 handler —— 给"用自己名字的库模块"用。

    为什么需要：`setup_logging(name)` 只管**那个名字**的 logger，而且 `propagate=False`；
    像 `tools/dev/v4_stub.py` 这种库模块用的是 `logging.getLogger("t4demo.stub")`，
    没人给根配 handler 时，它的 INFO 会被 `logging.lastResort`（只处理 WARNING+）
    **静默丢掉**，或者格式与别处不一致。一句话：工具脚本里用它开头就行。
    """
    root = logging.getLogger()
    root.setLevel(normalize_level(level))
    for handler in [h for h in root.handlers if getattr(h, _MARK, False)]:
        root.removeHandler(handler)
        handler.close()
    target = _console_stream(None)
    stream_handler = logging.StreamHandler(target)
    want_color = color_enabled(target) if color is None else bool(color)
    stream_handler.setFormatter(
        ColorFormatter(LOG_FORMAT, DATE_FORMAT) if want_color
        else logging.Formatter(LOG_FORMAT, DATE_FORMAT)
    )
    setattr(stream_handler, _MARK, True)
    root.addHandler(stream_handler)
    return root


def close_handlers(logger: logging.Logger) -> None:
    """关闭并移除**本模块加过的** handler，释放日志文件句柄。

    为什么需要：Windows 上文件被占用就删不掉/移不动它所在的目录。
    节点退出时释放自己的日志文件，是"干净退出"的一部分
    （不然测试或部署脚本清理日志目录时会静默失败）。
    """
    for handler in [h for h in logger.handlers if getattr(h, _MARK, False)]:
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001 - 关不掉也不能让退出流程炸
            pass


def utf8_writer(stream: Any = None) -> io.TextIOBase:
    """给 `print` 用的 UTF-8 兜底 writer（CLI 直接输出 JSON/中文时用）。"""
    target = stream if stream is not None else sys.stdout
    if getattr(target, "encoding", "").lower().replace("-", "") == "utf8":
        return target
    reconfigure = getattr(target, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
            return target
        except (ValueError, OSError, TypeError):
            pass
    return target
