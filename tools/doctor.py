"""CoyoteLink 环境自检。

用法（在项目根目录执行）:
    python tools/doctor.py

检查：Python 版本 / 依赖包 / 模型文件 / 代理连通性 / 关键模块可导入性。
任何一项 FAIL 都会给出修复建议。
"""

from __future__ import annotations

import importlib.util
import os
import socket
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _force_utf8() -> None:
    """入口处强制 stdout/stderr 走 UTF-8。

    ⚠️ 刻意**不 import `agent.*`**：doctor 的价值就在于"依赖缺了也能跑并告诉你缺什么"，
    而 `agent.core` 一导入就会 `import zenoh` → 缺包时直接崩，等于没有自检。
    也刻意**不用**别处的实现：这是本项目里唯一允许重复的一小段（同 `start.ps1` 的注释所说）。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 拿不到就凑合（比崩了强）
            pass


_force_utf8()

MODELS_DIR = os.path.join(ROOT, "models")

PROXY_HOST, PROXY_PORT = "127.0.0.1", 7897

OK, FAIL, WARN = "OK  ", "FAIL", "WARN"

# (导入名, pip 名, 说明, 是否必需)
PACKAGES: list[tuple[str, str, str, bool]] = [
    ("zenoh", "eclipse-zenoh", "消息总线／中间件 —— 所有节点都依赖它（缺了什么节点都起不来）", True),
    ("mediapipe", "mediapipe", "姿态/面部感知（M1）", True),
    ("cv2", "opencv-python", "摄像头采集（T2）", True),
    ("numpy", "numpy", "数值计算", True),
    ("bleak", "bleak", "BLE 客户端：爪印/心率（T7/M9）", True),
    ("serial", "pyserial", "ESP32 串口链路（T1 假郊狼主机）", True),
    ("winrt", "winrt-runtime", "Windows BLE 外设（T1 模拟郊狼）", True),
    ("dglab_kit_python", "dglab-kit-python", "郊狼 V4 官方 SDK（T4）", True),
    ("fastapi", "fastapi", "后端框架", True),
    ("uvicorn", "uvicorn", "ASGI 服务器", True),
    ("websockets", "websockets", "WebSocket 传输（T3）", True),
    ("cryptography", "cryptography", "房间加密（T3）", True),
    ("qrcode", "qrcode", "配对二维码（T4）", False),
    ("PIL", "pillow", "图像处理", False),
    ("pytest", "pytest", "单元测试", False),
    ("aiortc", "aiortc", "WebRTC DataChannel（T3 M-C，可后补）", False),
    ("sherpa_onnx", "sherpa-onnx", "语音识别：转写 + 关键词（T6；缺了只做音量分级）", False),
]

MODELS: list[tuple[str, int, str]] = [
    ("pose_landmarker_lite.task", 4000, "姿态轻量模型（默认）"),
    ("pose_landmarker_full.task", 7000, "姿态高精度模型（备用）"),
    ("face_landmarker.task", 2500, "面部模型（表情/头姿）"),
]

#: 语音识别模型（T6）：是**一个目录**而不是单文件，单独查
ASR_MODEL_DIR = "sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23"
#: 解压后必须存在的文件（缺一个就算没装好）
ASR_MUST_HAVE = (
    "tokens.txt",
    "encoder-epoch-99-avg-1.int8.onnx",
    "decoder-epoch-99-avg-1.int8.onnx",
    "joiner-epoch-99-avg-1.int8.onnx",
)

results: list[tuple[str, str, str]] = []


def record(level: str, title: str, detail: str = "") -> None:
    results.append((level, title, detail))


def check_python() -> None:
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= (3, 13):
        record(OK, f"Python {ver}", sys.executable)
    elif (v.major, v.minor) >= (3, 10):
        record(WARN, f"Python {ver}", "建议 3.13（dglab-kit-python 要求 >=3.13）")
    else:
        record(FAIL, f"Python {ver}", "版本过低，需要 3.13+")


def check_packages() -> None:
    for mod, pip_name, desc, required in PACKAGES:
        try:
            found = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            found = False
        if found:
            record(OK, f"包 {pip_name}", desc)
        elif required:
            record(FAIL, f"包 {pip_name}", f"{desc} —— 安装：pip install {pip_name}")
        else:
            record(WARN, f"包 {pip_name}", f"{desc}（可选，需要时再装）")


def check_models() -> None:
    for name, min_kb, desc in MODELS:
        path = os.path.join(MODELS_DIR, name)
        if not os.path.exists(path):
            record(FAIL, f"模型 {name}", f"{desc} —— 运行：python tools/download_models.py")
            continue
        size_kb = os.path.getsize(path) / 1024
        if size_kb < min_kb:
            record(FAIL, f"模型 {name}", f"仅 {size_kb:.0f} KB，疑似不完整 —— 用 --force 重下")
        else:
            record(OK, f"模型 {name}", f"{size_kb/1024:.1f} MB · {desc}")

    # 语音识别（T6）：是一个目录，检查关键文件是否都在
    asr_dir = os.path.join(MODELS_DIR, ASR_MODEL_DIR)
    if not os.path.isdir(asr_dir):
        record(WARN, "模型 语音识别",
               "未下载（只影响转写/关键词，音量分级照常）—— 运行：python tools/download_models.py")
        return
    missing = [f for f in ASR_MUST_HAVE if not os.path.exists(os.path.join(asr_dir, f))]
    if missing:
        record(WARN, "模型 语音识别", f"缺文件 {missing} —— 用 --force 重下")
        return
    total = sum(
        os.path.getsize(os.path.join(asr_dir, f))
        for f in os.listdir(asr_dir)
        if os.path.isfile(os.path.join(asr_dir, f))
    )
    record(OK, "模型 语音识别", f"{total/1024/1024:.1f} MB · 中文流式（转写 + 关键词）")


def check_proxy() -> None:
    try:
        with socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=2):
            record(OK, f"代理 {PROXY_HOST}:{PROXY_PORT}", "端口可达（下载国外资源需要）")
    except OSError:
        record(WARN, f"代理 {PROXY_HOST}:{PROXY_PORT}", "不可达；若无需翻墙可忽略")


def check_workspace() -> None:
    # 运行必需的目录：缺了就是残缺安装 → 告警
    for rel in ["agent", "web", "docs", "models", "tools"]:
        path = os.path.join(ROOT, rel)
        if os.path.isdir(path):
            record(OK, f"目录 {rel}/", "")
        else:
            record(WARN, f"目录 {rel}/", "缺失")
    # 开发期目录：**发行版里本来就没有** → 缺失不算问题（不然每次自检都刷两条假警告）
    for rel in ["tests", "reference", "vendor"]:
        path = os.path.join(ROOT, rel)
        if os.path.isdir(path):
            record(OK, f"目录 {rel}/", "开发资料" if rel != "vendor" else "随包的 SDK 源码")


def main() -> int:
    print("=" * 62)
    print(" CoyoteLink 环境自检")
    print("=" * 62)

    check_python()
    check_packages()
    check_models()
    check_proxy()
    check_workspace()

    print()
    for level, title, detail in results:
        line = f"[{level}] {title}"
        if detail:
            line += f"  ——  {detail}"
        print(line)

    fails = [r for r in results if r[0] == FAIL]
    warns = [r for r in results if r[0] == WARN]
    print()
    print("-" * 62)
    print(f"通过 {len(results) - len(fails) - len(warns)} · 警告 {len(warns)} · 失败 {len(fails)}")
    if fails:
        print("\n必须先解决：")
        for _, title, detail in fails:
            print(f"  · {title} —— {detail}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
