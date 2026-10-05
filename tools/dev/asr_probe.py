# -*- coding: utf-8 -*-
"""验证 sherpa-onnx 流式识别（用模型自带的 test_wavs，不需要麦克风）。"""
from __future__ import annotations

import sys
import wave
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(r"C:\Users\zsy\Desktop\web控制")
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from agent.nodes.audio.asr import (  # noqa: E402
    AsrSettings,
    StreamingAsr,
    find_model_dir,
    match_keywords,
)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as fh:
        rate = fh.getframerate()
        ch = fh.getnchannels()
        width = fh.getsampwidth()
        raw = fh.readframes(fh.getnframes())
    if width != 2:
        raise SystemExit(f"只支持 16bit PCM，得到 {width * 8}bit")
    data = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, rate


def main() -> int:
    model_dir = find_model_dir(ROOT / "models")
    print("模型目录：", model_dir)
    if model_dir is None:
        print("✗ 没找到模型")
        return 1

    asr = StreamingAsr(AsrSettings(model_dir=model_dir, num_threads=2))
    asr.load()
    print("就绪：", asr.ready, "错误：", asr.error or "（无）")
    if not asr.ready:
        return 1

    wavs = sorted((model_dir / "test_wavs").glob("*.wav"))
    for path in wavs:
        if path.name.startswith("8k"):     # 8k 那个是低采样率示例，跳过
            continue
        audio, rate = read_wav(path)
        asr.finish()                       # 每段独立
        text = ""
        step = rate // 10                  # 100ms 一块，模拟流式
        for i in range(0, len(audio), step):
            text = asr.accept(audio[i:i + step], rate)
        final = asr.finish()
        hits = match_keywords(final, AsrSettings(model_dir=model_dir).keywords)
        print(f"\n{path.name}（{len(audio) / rate:.1f}s @{rate}Hz）")
        print(f"  识别：{final!r}")
        print(f"  关键词命中：{hits or '（无）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
