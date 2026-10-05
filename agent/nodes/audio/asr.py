"""语音识别（T6，需求 1e）—— **sherpa-onnx 流式中文** + 关键词命中。

需求原话::

    "用麦克风获取人声，用模型识别文字或者淫叫求饶等信息传到 web 控制面板"

所以这里做两件事：

1. **转写**：流式识别，边说边出字（`text`）；
2. **关键词命中**：从转写里匹配"求饶/喊停"这类词（`keywords`）——
   ⚠️ 那是**语义**层面的判断，和"叫得多大声"（`level`/`event`，音量层面）是两回事，
   两者都往面板送，让控制方自己看。

模型：`sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23`（**int8 量化后约 25MB**，
符合"轻量化"要求）。下载见 `tools/download_models.py` 或 README。

设计要点
--------

* **不在音频回调线程里跑**：`sounddevice` 的回调必须尽快返回（否则丢帧/爆音），
  所以音频块进队列，识别在**独立线程**里做（见 `audio/node.py` 的 `_asr_loop`）。
* **模型缺失不是错误**：没装/没下 → `ready=False`，节点照常只做音量分级（优雅降级）。
* 采样率必须 **16kHz**（模型要求）；`accept()` 会**重采样**兜底，但最好让麦克风直接开 16k。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "AsrError",
    "AsrSettings",
    "StreamingAsr",
    "DEFAULT_KEYWORDS",
    "MODEL_DIR_NAME",
    "match_keywords",
    "find_model_dir",
]

log = logging.getLogger("coyotelink.audio.asr")

#: 随包下载的模型目录名（放在 `<项目>/models/` 下）
MODEL_DIR_NAME = "sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23"

#: 默认关键词：喊停/求饶这类"语气词"。
#: ⚠️ 刻意**不放**"别"这种单字 —— 太容易在正常句子里误命中。
DEFAULT_KEYWORDS: tuple[str, ...] = (
    "不要", "不行", "停下", "停", "求你了", "求你", "饶了我", "饶命",
    "受不了", "轻点", "慢点", "太疼", "疼", "救命", "放过我",
)


class AsrError(RuntimeError):
    """模型加载 / 识别失败。"""


def match_keywords(text: str, keywords: Iterable[str]) -> list[str]:
    """从一段文本里找出命中的关键词（去重、保序）。"""
    if not text:
        return []
    hits: list[str] = []
    for kw in keywords:
        if kw and kw in text and kw not in hits:
            hits.append(kw)
    return hits


def find_model_dir(models_dir: str | Path, *, name: str = MODEL_DIR_NAME) -> Path | None:
    """在 ``models_dir`` 下找模型目录；找不到返回 ``None``。"""
    base = Path(models_dir)
    candidate = base / name
    if (candidate / "tokens.txt").exists():
        return candidate
    # 兜底：任何名字里带 streaming-zipformer-zh 的目录都认（免得改名后失效）
    if base.is_dir():
        for child in sorted(base.iterdir()):
            if child.is_dir() and "streaming" in child.name and "zh" in child.name:
                if (child / "tokens.txt").exists():
                    return child
    return None


@dataclass
class AsrSettings:
    """识别器参数。"""

    model_dir: Path
    num_threads: int = 2
    use_int8: bool = True
    sample_rate: int = 16000
    decoding_method: str = "greedy_search"
    #: 端点检测：一句话说完（静音够久）就定稿
    endpoint: bool = True
    rule1_min_trailing_silence: float = 2.4
    rule2_min_trailing_silence: float = 1.2
    #: 关键词（只做**文本匹配**，不参与解码）
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS
    #: 热词文件（可选）：让模型对某些词更敏感，格式见 sherpa-onnx 文档
    hotwords_file: str = ""
    hotwords_score: float = 1.5


class StreamingAsr:
    """流式识别器封装（线程内使用，**非线程安全**）。

    典型用法（在独立线程里）::

        asr = StreamingAsr(settings); asr.load()
        if asr.ready:
            text = asr.accept(block)          # 边喂边出字
            if asr.endpoint_hit():            # 一句结束
                final = asr.finish()
    """

    def __init__(self, settings: AsrSettings) -> None:
        self.settings = settings
        self._recognizer: Any = None
        self._stream: Any = None
        self._error: str = ""
        self._text: str = ""

    # ── 加载 ────────────────────────────────────────────────
    @property
    def ready(self) -> bool:
        return self._recognizer is not None and self._stream is not None

    @property
    def error(self) -> str:
        return self._error

    @property
    def text(self) -> str:
        return self._text

    def load(self) -> None:
        """加载模型；失败**不抛到调用方**（记在 :attr:`error`，节点降级运行）。"""
        model_dir = Path(self.settings.model_dir)
        tokens = model_dir / "tokens.txt"
        suffix = ".int8.onnx" if self.settings.use_int8 else ".onnx"
        encoder = model_dir / f"encoder-epoch-99-avg-1{suffix}"
        decoder = model_dir / f"decoder-epoch-99-avg-1{suffix}"
        joiner = model_dir / f"joiner-epoch-99-avg-1{suffix}"

        missing = [p.name for p in (tokens, encoder, decoder, joiner) if not p.exists()]
        if missing:
            self._error = f"模型文件缺失：{missing}（目录 {model_dir}）"
            log.warning("语音识别停用 —— %s", self._error)
            return

        try:
            import sherpa_onnx  # 延迟 import：没装也不影响节点只做音量
        except ImportError as exc:
            self._error = f"未安装 sherpa-onnx：{exc}"
            log.warning("语音识别停用 —— %s", self._error)
            return

        try:
            kwargs: dict[str, Any] = {
                "tokens": str(tokens),
                "encoder": str(encoder),
                "decoder": str(decoder),
                "joiner": str(joiner),
                "num_threads": int(self.settings.num_threads),
                "sample_rate": int(self.settings.sample_rate),   # ★ 必须 int，float 会被 C++ 拒绝
                "feature_dim": 80,
                "decoding_method": self.settings.decoding_method,
                "enable_endpoint_detection": bool(self.settings.endpoint),
                "rule1_min_trailing_silence": self.settings.rule1_min_trailing_silence,
                "rule2_min_trailing_silence": self.settings.rule2_min_trailing_silence,
            }
            if self.settings.hotwords_file:
                kwargs["hotwords_file"] = self.settings.hotwords_file
                kwargs["hotwords_score"] = self.settings.hotwords_score
            self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(**kwargs)
            self._stream = self._recognizer.create_stream()
        except Exception as exc:  # noqa: BLE001 - 模型坏了也不能打死节点
            self._error = f"加载模型失败：{exc}"
            self._recognizer = None
            self._stream = None
            log.warning("语音识别停用 —— %s", self._error)
            return
        log.info("语音识别就绪：%s（int8=%s，%d 线程）",
                 model_dir.name, self.settings.use_int8, self.settings.num_threads)

    # ── 识别 ────────────────────────────────────────────────
    def accept(self, samples: Any, sample_rate: int | None = None) -> str:
        """喂一块音频（``float32``，范围 -1~1），返回**当前累积文本**。"""
        if not self.ready:
            return ""
        try:
            import numpy as np

            arr = np.asarray(samples, dtype="float32").reshape(-1)
            if arr.size == 0:
                return self._text
            rate = int(sample_rate or self.settings.sample_rate)
            want = int(self.settings.sample_rate)
            if rate != want:                       # 兜底重采样（最好让麦克风直接开 16k）
                arr = _resample(arr, rate, want)
            self._stream.accept_waveform(want, arr)
            while self._recognizer.is_ready(self._stream):
                self._recognizer.decode_stream(self._stream)
            self._text = str(self._recognizer.get_result(self._stream) or "")
        except Exception as exc:  # noqa: BLE001 - 识别失败不能拖垮音频链路
            log.debug("识别失败（忽略本块）：%s", exc)
        return self._text

    def endpoint_hit(self) -> bool:
        """这一句是否说完（端点检测命中）？"""
        if not self.ready:
            return False
        try:
            return bool(self._recognizer.is_endpoint(self._stream))
        except Exception:  # noqa: BLE001
            return False

    def finish(self) -> str:
        """定稿并**重置**，准备下一句；返回这一句的文本。"""
        text = self._text
        if self.ready:
            try:
                self._recognizer.reset(self._stream)
            except Exception:  # noqa: BLE001
                pass
        self._text = ""
        return text

    def keywords_in(self, text: str | None = None) -> list[str]:
        return match_keywords(self._text if text is None else text, self.settings.keywords)

    def close(self) -> None:
        self._stream = None
        self._recognizer = None


def _resample(arr: Any, src_rate: int, dst_rate: int) -> Any:
    """线性插值重采样（够用；要更好可以用 scipy.signal.resample_poly）。"""
    import numpy as np

    if src_rate == dst_rate or arr.size == 0:
        return arr
    n_out = max(1, int(round(arr.size * dst_rate / float(src_rate))))
    idx = np.linspace(0.0, arr.size - 1, n_out, dtype="float64")
    lo = np.floor(idx).astype("int64")
    hi = np.minimum(lo + 1, arr.size - 1)
    frac = (idx - lo).astype("float32")
    return (arr[lo] * (1.0 - frac) + arr[hi] * frac).astype("float32")
