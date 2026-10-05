"""把工作区打包成**正式发行版**（默认输出 `<project_root>/webDG`）。

    python tools/build_release.py                 # 原地覆盖式打包（不动你的 .venv/data/logs）
    python tools/build_release.py --clean         # 先清干净再打（**保留** .venv / data / logs / config.toml）
    python tools/build_release.py --no-models     # 不带模型（省 ~100MB，首次启动再下）
    python tools/build_release.py --out D:\\webDG  # 打到别处（U 盘 / 另一台机器）

发行版里有什么、为什么
----------------------

* **agent/ web/ tools/ firmware/ docs/** —— 运行必需 + 文档；
* **models/** —— 默认打包：拷到别的机器**不用联网**就能跑满功能（MediaPipe + 语音识别）；
* **vendor/dglab-kit-python** —— 郊狼 V4 官方 SDK 未上 PyPI，只能带源码；
  不带的话 `coyote_v4` 会降级成"手动传输模式"（其它节点不受影响）。
* **start.cmd / start.ps1 / stop.cmd** —— 一键启动（首次启动自动建 `.venv` + 装依赖）；
* **VERSION / build-info.json** —— 这个包是哪个提交、什么时候打的（排障第一问）；
* **requirements.txt** —— 去掉 `pytest`（发行版不需要，省一个包）。

不带什么：`tests/`、`reference/`（除上面那份 SDK）、`.git`、`data/`、`logs/`、
`__pycache__/`、`.venv/`、`docs/tasks/`。

⚠️ 重复打包是**原地覆盖**：不会删你的 `.venv` / `data/` / `logs/` / `agent/config.toml`
（那是用户在发行版里攒下的运行状态，删了等于让人重装）。要真清干净加 `--clean`
（它也会保留上面那几样）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 直接整棵复制的目录（相对项目根）
COPY_DIRS: tuple[str, ...] = ("agent", "web", "tools", "firmware", "docs")

#: 打包时一律跳过的名字（缓存/环境/版本库/编译产物 —— 体积没意义且会串味）
SKIP_NAMES: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".git",
        ".venv",
        "venv",
        ".pio",          # PlatformIO 编译缓存（实测 64MB，纯构建产物）
        ".platformio",
    }
)
SKIP_SUFFIXES: tuple[str, ...] = (".pyc", ".pyo", ".log")

#: 单个文件复制（相对项目根 → 发行版内路径）
COPY_FILES: tuple[tuple[str, str], ...] = (
    ("agent/config.example.toml", "agent/config.example.toml"),
    ("tools/relay.py", "tools/relay.py"),
)

#: 启动器（版本控制里的源 → 发行版根目录）
LAUNCHER_FILES: tuple[tuple[str, str], ...] = (
    ("deploy/launcher/start.cmd", "start.cmd"),
    ("deploy/launcher/start.ps1", "start.ps1"),
    ("deploy/launcher/stop.cmd", "stop.cmd"),
    ("deploy/launcher/stop.ps1", "stop.ps1"),
)

#: V4 SDK 源码（`reference/dglab-kit-python` → `vendor/dglab-kit-python`）
SDK_SRC = Path("reference/dglab-kit-python")
SDK_DST = Path("vendor/dglab-kit-python")

#: `--clean` 时也**绝不删**的东西（用户在发行版里攒下的运行状态）
PRESERVE: frozenset[str] = frozenset({".venv", "data", "logs", "vendor"})


def _git(*args: str) -> str:
    """跑一条 git 命令取输出；**任何失败都当"没有"**（打包不该因为 git 不在就挂）。

    ⚠️ 必须显式 `encoding="utf-8"`：默认会按本机 locale（GBK）解码，而仓库里有**中文文件名**，
    `subprocess` 的 reader 线程会抛 `UnicodeDecodeError` → 打包直接失败（实测踩过）。
    """
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    return (out.stdout or "").strip()


def _ignore(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in SKIP_NAMES or n.endswith(SKIP_SUFFIXES)}


def _copy_tree(src: Path, dst: Path, *, log: list[str]) -> tuple[int, int]:
    if not src.is_dir():
        log.append(f"  ! 跳过（不存在）：{src.relative_to(ROOT)}")
        return 0, 0
    count = size = 0
    for item in sorted(src.rglob("*")):
        rel = item.relative_to(src)
        if any(part in SKIP_NAMES for part in rel.parts):
            continue
        if item.is_dir():
            continue
        if item.suffix in SKIP_SUFFIXES:
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        count += 1
        size += item.stat().st_size
    return count, size


def _release_requirements(out_dir: Path) -> tuple[int, int]:
    """发行版 requirements：去掉 pytest / 开发期注释，加一段"发行版说明"头。"""
    src = ROOT / "requirements.txt"
    if not src.is_file():
        return 0, 0
    kept: list[str] = []
    for line in src.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("pytest"):
            continue
        # 纯注释里的"安装指引"在发行版里已经有 start.cmd 了，去掉更清爽
        if stripped.startswith("#") and (
            "pip install" in stripped or "tools/doctor.py" in stripped or "download_models" in stripped
        ):
            continue
        kept.append(line)
    header = [
        "# webDG 发行版依赖（由 tools/build_release.py 生成）",
        "#",
        "# 正常用法：双击 start.cmd —— 它会自动建 .venv 并装这些依赖。",
        "# 手动装：.venv\\Scripts\\python.exe -m pip install -r requirements.txt \\",
        "#             --proxy http://127.0.0.1:7897 --index-url https://pypi.org/simple",
        "",
    ]
    text = "\n".join(header + kept).rstrip() + "\n"
    path = out_dir / "requirements.txt"
    path.write_text(text, encoding="utf-8")
    return 1, len(text.encode("utf-8"))


def _write_release_readme(out_dir: Path, *, version: str, commit: str, built: str) -> int:
    text = f"""# webDG —— 远程联动控制系统（发行版 {version}）

> 摄像头 + 麦克风 → **本地**轻量模型 → 只传结构化特征 → Web 面板远程查看与控制，
> 并联动 DG-LAB 郊狼。**原始音视频绝不离开被控端机器**（链路上只有骨骼坐标、
> 表情标签、识别文本和事件 —— 抓包也拿不到画面）。

打包于 {built}　源码提交 `{commit or "（非 git 工作区）"}`

---

## 一键启动

**双击 `start.cmd`** 就行。首次启动它会：

1. 在本目录建独立环境 `.venv`（**不碰**你系统里的 Python）；
2. 装依赖（自动探测本机代理 `http://127.0.0.1:7897`；探不到就直连试）；
3. 装郊狼 V4 SDK（`vendor\\dglab-kit-python`，官方未上 PyPI）；
4. 从模板生成 `agent\\config.toml`；
5. 起全部节点（独立窗口，日志彩色，Ctrl+C 停止）并打开控制面板。

之后每次启动**秒起**（环境已就绪会跳过安装）。

| 我想要 | 命令 |
|---|---|
| 只装环境 + 自检，不起节点 | `start.cmd -Setup` |
| 依赖装坏了 / 换机器 | `start.cmd -Reinstall` |
| **跨机对端（B 端）** | `start.cmd -Role b` |
| 面板换端口 | `start.cmd -Port 8888` |
| 不起本机的 V4 中继 | `start.cmd -NoRelay` |
| 停止 | 节点窗口里 Ctrl+C，或双击 `stop.cmd` |

---

## 控制面板

启动后自动打开，也可以在浏览器里手动进：<http://127.0.0.1:8780>

- **房间**：点「创建房间」生成一个 6 位房间码，把这码给对端 ——
  **跨机两端在各自面板上选同一个房间码**即可（房间隔离 + 跨机加密链路
  都由这一个码派生，不用再配别的）。
- **连接**：面板上「🔌 连接」三盏灯 = 郊狼 Relay / ESP32 假主机 / 手机 App，
  绿=通、红=起不来、黄=正在试。
- **设备**：摄像头 / 麦克风 / ESP32 串口都能在面板上下拉切换（热切换，不用重启）。

---

## 跨机（两台机器）

1. 两台都装这个发行版，各自 `start.cmd`；
2. **一端正 `a`、一端 `b`**：B 端用 `start.cmd -Role b`（或编辑 `agent\\config.toml`
   的 `[bridge] role`）。两端 role 相同会导致链路 key 对不上、**跨机静默不通**；
3. 编辑两端的 `agent\\config.toml`，让 `[bridge] listen` / `[bridge] connect`
   指向对方（走公网建议套樱花穿透，见 `docs\\樱花穿透部署.md`）；
4. 两端在面板上选**同一个房间码**。

不通时按 `vendor` 之外的排查表来：看节点窗口里有没有
`本机 → 跨机 xxx` / `跨机 → 本机 xxx`（把日志级别调到 DEBUG 才打）。

---

## 硬件

| 东西 | 怎么配 |
|---|---|
| ESP32 假郊狼主机 | `agent\\config.toml` → `[sim_coyote] port = "COM18"`（插上板子后在面板的设备下拉里选它的串口也行；**别选蓝牙虚拟串口**，那种口能打开却收不到字节） |
| V4 中继 | 默认 `start.cmd` 会在本机起一个（`ws://127.0.0.1:9997?room=…`）；对端 relay 在别的机器就把 `[coyote_v4] url` 改成那个地址 |
| 郊狼真机 / 手机 App | 手机 App 扫控制端面板/中继打印的配对二维码接入 |

---

## 目录

```
webDG/
├── start.cmd / start.ps1 / stop.cmd   ← 一键启动 / 停止
├── agent/        代码根（12 个节点 + 框架）
│   └── config.toml           你的本机配置（首次启动自动生成，改它）
├── web/          控制面板（原生 JS，无构建步骤）
├── tools/        辅助脚本（relay.py 中继、doctor.py 自检、download_models.py 下模型）
├── models/       已打包的模型（姿态/表情 + 语音识别）
├── vendor/       郊狼 V4 SDK 源码
├── firmware/     ESP32 假郊狼固件源码
├── docs/         设计文档（含 樱花穿透部署）
├── data/         运行时状态（房间、设备选择…）—— 面板选过的东西记在这里
└── logs/         日志（**纯文本**；窗口里那份是彩色的）
```

## 日志

- 窗口里：**彩色** —— 级别配色（DEBUG 灰 / INFO 青 / WARNING 黄 / ERROR 红），
  节点名按名字配色，`开火`/`房间`/`连接`/`失败`/key 名都有独立颜色；
- `logs\\*.log`：同内容**纯文本**（`grep` / `less` 友好）；
- 想强制关掉颜色：`set COYOTELINK_COLOR=0`（或给 CLI 加 `--no-color`）。

## 常见问题

| 现象 | 原因 / 怎么办 |
|---|---|
| **弹窗问"是否允许 python.exe 访问网络"** | 那是 **Windows 防火墙**询问（不是要管理员权限）—— 中间件 Zenoh 要监听网卡才能发现对端。**只做本机演示：点"取消"也能跑**；要跨机：点"允许访问"并只勾**专用网络** |
| 双击就闪退 | 用 `start.cmd -Setup` 看报错；一般是没装 Python 3.10+ |
| 依赖装不上 | 代理没开或端口不同：`start.cmd -Proxy http://127.0.0.1:你的端口` |
| 面板打不开 | 等十几秒（12 个节点冷启动）；还不行看节点窗口的日志 |
| 郊狼 Relay 灯红 | 中继没起，或 `[coyote_v4] url` 没指对（含 `?room=` 要和中继一致） |
| ESP32 灯红 | 串口选错 / 板子没插 / 固件没烧（`firmware\\` 里有源码） |
| 听不到语音识别 | 模型在（已打包）→ 检查面板上麦克风选对没有；识别在独立线程跑，不影响音量条 |

---

> 这一份是**发行版**：只有运行必需 + 文档（不含单元测试与参考仓库）。
> 要开发/改代码，请用源码工作区（那边有 `tests/`、`reference/` 与任务书）。
"""
    path = out_dir / "README.md"
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def _write_dot_gitignore(out_dir: Path) -> int:
    text = "\n".join(
        [
            "# 运行时产物 / 本地环境 —— 都别进版本控制",
            ".venv/",
            "data/",
            "logs/",
            "__pycache__/",
            "*.py[cod]",
            "*.log",
            "agent/config.toml",
            "run-nodes.cmd",
            "",
        ]
    )
    (out_dir / ".gitignore").write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def _ensure_ps1_bom(path: Path) -> None:
    """给 `.ps1` 补 UTF-8 BOM —— **没 BOM 的脚本在 Windows PowerShell 5.1 里必炸**。

    实测：无 BOM 的 UTF-8 `.ps1` 会被 PS 5.1 按 ANSI(GBK) 解码，中文和 `═` 这类
    框线字符全乱码 → `Unexpected token` 一堆语法错误（脚本根本起不来）。
    `.cmd` **不能**加 BOM（cmd.exe 会把 BOM 当成命令的一部分）。
    """
    if path.suffix.lower() != ".ps1":
        return
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        return
    path.write_bytes(b"\xef\xbb\xbf" + data)


def build(out_dir: Path, *, clean: bool, with_models: bool) -> int:
    started = time.time()
    log: list[str] = []
    files = 0
    total = 0

    out_dir = out_dir.resolve()
    if out_dir == ROOT:
        log.append("  ✗ 输出目录不能是项目根（会把自己的源码覆盖掉）")
        print("\n".join(log))
        return 2

    if clean and out_dir.exists():
        for child in out_dir.iterdir():
            if child.name in PRESERVE:
                continue
            shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink()
        log.append("  · --clean：已清空（保留了 .venv / data / logs / vendor）")

    out_dir.mkdir(parents=True, exist_ok=True)

    for name in COPY_DIRS:
        count, size = _copy_tree(ROOT / name, out_dir / name, log=log)
        files += count
        total += size
        log.append(f"  · {name}/：{count} 个文件，{size / 1024:.0f} KB")

    if with_models:
        count, size = _copy_tree(ROOT / "models", out_dir / "models", log=log)
        files += count
        total += size
        log.append(f"  · models/：{count} 个文件，{size / 1024 / 1024:.1f} MB")
    else:
        log.append("  · models/：按 --no-models 跳过（首次启动请跑 tools/download_models.py）")

    # V4 SDK（源码，必须带）
    if (ROOT / SDK_SRC).is_dir():
        count, size = _copy_tree(ROOT / SDK_SRC, out_dir / SDK_DST, log=log)
        files += count
        total += size
        log.append(f"  · {SDK_DST}（郊狼 V4 SDK 源码）：{count} 个文件")
    else:
        log.append(f"  ! 缺 {SDK_SRC} → 发行版里没有 V4 SDK，coyote_v4 会降级运行")

    for src, dst in COPY_FILES:
        source = ROOT / src
        if not source.is_file():
            log.append(f"  ! 缺 {src}")
            continue
        target = out_dir / dst
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        files += 1
        total += source.stat().st_size

    for src, dst in LAUNCHER_FILES:
        source = ROOT / src
        if not source.is_file():
            log.append(f"  ! 缺启动器文件 {src}")
            continue
        target = out_dir / dst
        shutil.copy2(source, target)
        _ensure_ps1_bom(target)      # ★ 见函数注释：PS 5.1 没 BOM 就乱码
        files += 1
        total += source.stat().st_size
        total += 3 if dst.lower().endswith(".ps1") else 0
    log.append("  · 启动器：start.cmd / start.ps1 / stop.cmd")

    files += 1
    _, req_bytes = _release_requirements(out_dir)
    total += req_bytes
    total += _write_dot_gitignore(out_dir)

    # 发行版自述（webDG/README.md，不是工作区那份）
    version = _git("describe", "--tags", "--always", "--dirty") or time.strftime("%Y%m%d")
    commit = _git("rev-parse", "--short", "HEAD")
    built = time.strftime("%Y-%m-%d %H:%M:%S")
    (out_dir / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    files += 1
    total += _write_release_readme(out_dir, version=version, commit=commit, built=built)
    files += 1

    info = {
        "version": version,
        "built_at": built,
        "source_commit": commit or None,
        "source_dirty": bool(_git("status", "--porcelain")),
        "with_models": with_models,
        "files": files,
        "bytes": total,
        "built_by": "tools/build_release.py",
    }
    (out_dir / "build-info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    elapsed = time.time() - started
    print("\n".join(log))
    print("")
    print(f"  发行版：{out_dir}")
    print(f"  版本：{version}" + (f"（源码提交 {commit}）" if commit else ""))
    print(f"  体积：{total / 1024 / 1024:.1f} MB，{files} 个文件，耗时 {elapsed:.1f}s")
    print("  用法：双击 start.cmd（首次启动自动建 .venv + 装依赖）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包 webDG 发行版")
    parser.add_argument("--out", default=str(ROOT / "webDG"), help="输出目录（默认 <项目根>/webDG）")
    parser.add_argument("--clean", action="store_true",
                        help="先清空输出目录（保留 .venv / data / logs / vendor）")
    parser.add_argument("--no-models", action="store_true", help="不带模型（省约 100MB）")
    args = parser.parse_args(argv)
    return build(Path(args.out), clean=args.clean, with_models=not args.no_models)


if __name__ == "__main__":
    raise SystemExit(main())
