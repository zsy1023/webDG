"""下载模型到 models/ 目录。

用法（在项目根目录执行）:
    python tools/download_models.py            # 只下缺失的（含语音识别模型）
    python tools/download_models.py --force    # 全部重下
    python tools/download_models.py --list     # 只列出清单
    python tools/download_models.py --no-asr   # 跳过语音识别（GitHub 上那个比较大）

注意：本机下载国外资源必须走代理，默认 http://127.0.0.1:7897
      可用环境变量 HTTPS_PROXY 覆盖。
"""

from __future__ import annotations

import argparse
import os
import sys
import tarfile
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(ROOT, "models")


def _force_utf8() -> None:
    """入口处强制 stdout/stderr 走 UTF-8（同 `doctor.py`：**不 import `agent.*`**，
    这样依赖没装齐时也能跑起来把话说清楚）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


_force_utf8()

DEFAULT_PROXY = "http://127.0.0.1:7897"
_BASE = "https://storage.googleapis.com/mediapipe-models"

MODELS: dict[str, dict] = {
    "pose_landmarker_lite.task": {
        "url": f"{_BASE}/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task",
        "min_kb": 4000,
        "desc": "姿态（33 点，轻量，默认用这个）",
    },
    "pose_landmarker_full.task": {
        "url": f"{_BASE}/pose_landmarker/pose_landmarker_full/float16/1/pose_landmarker_full.task",
        "min_kb": 7000,
        "desc": "姿态（33 点，精度更高，备用）",
    },
    "face_landmarker.task": {
        "url": f"{_BASE}/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
        "min_kb": 2500,
        "desc": "面部（478 点 + 52 blendshapes + 头姿矩阵）",
    },
}

# ── 语音识别（T6）：sherpa-onnx 流式中文模型，**压缩包 + 解压**，和上面单文件的流程不同 ──
ASR_NAME = "sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23"
ASR_TARBALL = f"{ASR_NAME}.tar.bz2"
ASR_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{ASR_TARBALL}"
ASR_DESC = "语音识别（中文流式，int8 约 25MB）"
#: 解压后必须存在的文件（缺一个就算没装好）
ASR_MUST_HAVE = (
    "tokens.txt",
    "encoder-epoch-99-avg-1.int8.onnx",
    "decoder-epoch-99-avg-1.int8.onnx",
    "joiner-epoch-99-avg-1.int8.onnx",
)


def _opener(proxy: str | None) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def download(name: str, spec: dict, proxy: str | None, force: bool) -> bool:
    dest = os.path.join(MODELS_DIR, name)
    min_bytes = spec["min_kb"] * 1024

    if not force and os.path.exists(dest) and os.path.getsize(dest) >= min_bytes:
        size_mb = os.path.getsize(dest) / 1024 / 1024
        print(f"  [skip] {name:<30} 已存在 {size_mb:.1f} MB")
        return True

    print(f"  [get ] {name:<30} {spec['desc']}")
    try:
        req = urllib.request.Request(spec["url"], headers={"User-Agent": "coyotelink-setup"})
        with _opener(proxy).open(req, timeout=180) as resp:  # noqa: S310
            data = resp.read()
    except urllib.error.URLError as exc:
        print(f"  [FAIL] {name}: {exc}")
        print("         检查代理是否开启，或显式指定：--proxy http://127.0.0.1:7897")
        return False

    if len(data) < min_bytes:
        print(f"  [FAIL] {name}: 只下到 {len(data)/1024:.0f} KB，疑似不完整")
        return False

    os.makedirs(MODELS_DIR, exist_ok=True)
    with open(dest, "wb") as fh:
        fh.write(data)
    print(f"  [ ok ] {name:<30} {len(data)/1024/1024:.1f} MB")
    return True


def asr_ready() -> bool:
    """语音识别模型是否已解压好（只看关键文件在不在）。"""
    base = os.path.join(MODELS_DIR, ASR_NAME)
    return all(os.path.exists(os.path.join(base, f)) for f in ASR_MUST_HAVE)


def download_asr(proxy: str | None, force: bool = False) -> bool:
    """下载并解压语音识别模型（约 70MB 压缩包 → 25MB int8 权重）。"""
    if not force and asr_ready():
        print(f"  [skip] {ASR_NAME} 已就绪")
        return True

    tmp = os.path.join(MODELS_DIR, ASR_TARBALL)
    os.makedirs(MODELS_DIR, exist_ok=True)
    print(f"  [get ] {ASR_NAME} {ASR_DESC}（GitHub，较慢）")
    try:
        req = urllib.request.Request(ASR_URL, headers={"User-Agent": "coyotelink-setup"})
        with _opener(proxy).open(req, timeout=600) as resp, open(tmp, "wb") as fh:  # noqa: S310
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
    except (urllib.error.URLError, OSError) as exc:
        print(f"  [FAIL] {ASR_NAME}: {exc}")
        print("         GitHub 走代理经常慢，可以挂代理后手动下载：")
        print(f"         {ASR_URL}")
        return False

    size_mb = os.path.getsize(tmp) / 1024 / 1024
    if size_mb < 30:                     # 该包约 70MB，太小就是下坏了（多半是 HTML 错误页）
        print(f"  [FAIL] {ASR_NAME}: 只下到 {size_mb:.1f} MB，疑似不完整")
        return False

    print(f"  [unz ] {ASR_TARBALL} {size_mb:.1f} MB → 解压中…")
    try:
        with tarfile.open(tmp, "r:bz2") as tf:
            tf.extractall(MODELS_DIR)      # noqa: S202 - 来源固定为官方 release
    except (tarfile.TarError, OSError) as exc:
        print(f"  [FAIL] 解压失败：{exc}")
        return False
    finally:
        try:
            os.remove(tmp)                 # 压缩包留着没用（省 70MB）
        except OSError:
            pass

    if not asr_ready():
        print(f"  [FAIL] 解压后缺少关键文件，检查 {os.path.join(MODELS_DIR, ASR_NAME)}")
        return False
    print(f"  [ ok ] {ASR_NAME} 就绪")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="下载项目模型（MediaPipe + 语音识别）")
    ap.add_argument("--force", action="store_true", help="强制重新下载")
    ap.add_argument("--list", action="store_true", help="只列出清单")
    ap.add_argument("--no-asr", action="store_true", help="跳过语音识别模型（T6）")
    ap.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY") or DEFAULT_PROXY,
                    help="代理地址，传空字符串可禁用")
    args = ap.parse_args()

    if args.list:
        for name, spec in MODELS.items():
            print(f"{name:<32} {spec['desc']}")
        if not args.no_asr:
            print(f"{ASR_NAME:<32} {ASR_DESC}")
        return 0

    proxy = args.proxy or None
    print(f"目标目录: {MODELS_DIR}")
    print(f"代理:     {proxy or '（不使用）'}")
    print()

    results = {name: download(name, spec, proxy, args.force) for name, spec in MODELS.items()}
    if not args.no_asr:
        results[ASR_NAME] = download_asr(proxy, args.force)

    print()
    failed = [n for n, ok in results.items() if not ok]
    if failed:
        print(f"失败 {len(failed)} 个: {', '.join(failed)}")
        return 1
    print("全部就绪 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def _opener(proxy: str | None) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def download(name: str, spec: dict, proxy: str | None, force: bool) -> bool:
    dest = os.path.join(MODELS_DIR, name)
    min_bytes = spec["min_kb"] * 1024

    if not force and os.path.exists(dest) and os.path.getsize(dest) >= min_bytes:
        size_mb = os.path.getsize(dest) / 1024 / 1024
        print(f"  [skip] {name:<30} 已存在 {size_mb:.1f} MB")
        return True

    print(f"  [get ] {name:<30} {spec['desc']}")
    try:
        req = urllib.request.Request(spec["url"], headers={"User-Agent": "coyotelink-setup"})
        with _opener(proxy).open(req, timeout=180) as resp:  # noqa: S310
            data = resp.read()
    except urllib.error.URLError as exc:
        print(f"  [FAIL] {name}: {exc}")
        print("         检查代理是否开启，或显式指定：--proxy http://127.0.0.1:7897")
        return False

    if len(data) < min_bytes:
        print(f"  [FAIL] {name}: 只下到 {len(data)/1024:.0f} KB，疑似不完整")
        return False

    os.makedirs(MODELS_DIR, exist_ok=True)
    with open(dest, "wb") as fh:
        fh.write(data)
    print(f"  [ ok ] {name:<30} {len(data)/1024/1024:.1f} MB")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="下载 MediaPipe 模型")
    ap.add_argument("--force", action="store_true", help="强制重新下载")
    ap.add_argument("--list", action="store_true", help="只列出清单")
    ap.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY") or DEFAULT_PROXY,
                    help="代理地址，传空字符串可禁用")
    args = ap.parse_args()

    if args.list:
        for name, spec in MODELS.items():
            print(f"{name:<30} {spec['desc']}")
        return 0

    proxy = args.proxy or None
    print(f"目标目录: {MODELS_DIR}")
    print(f"代理:     {proxy or '（不使用）'}")
    print()

    results = {name: download(name, spec, proxy, args.force) for name, spec in MODELS.items()}

    print()
    failed = [n for n, ok in results.items() if not ok]
    if failed:
        print(f"失败 {len(failed)} 个: {', '.join(failed)}")
        return 1
    print("全部就绪 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
