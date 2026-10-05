"""blendshapes → 情绪（T6，需求 1c：把被控的表情以**文字/表情包**显示）。

输入是 MediaPipe FaceLandmarker 的 **52 个 blend shape 分数**（0~1），
输出是一个情绪标签 + 置信度。标签刻意用**前端已认识的那套英文名**
（见 `web/js/app.js` 的 `EMOTION_EMOJI`），免得两边对不上：

    neutral / happy / sad / angry / surprise / fear / disgust / contempt / pain

做法是**加权规则**而不是再训一个模型 —— 理由：

* 52 个 blendshape 本身就是"面部肌肉动作单元"（AU），情绪是它们的组合，规则可解释；
* 零新依赖、零额外推理开销（本来就要算 blendshapes）；
* 想要更好效果时，**换掉 :data:`RULES` 或整个 :func:`classify` 即可**，
  上层（face 节点）与协议都不用动。

⚠️ 这是"**看起来像什么**"而不是"**心里想什么**"：分类错了很正常，
  消费方（面板/判定引擎）别把它当事实，当提示用。
"""

from __future__ import annotations

from collections import Counter, deque
from typing import Any, Mapping

__all__ = [
    "EMOTIONS",
    "RULES",
    "RULES_VERSION",
    "DEFAULT_THRESHOLD",
    "scores",
    "classify",
    "top_k",
    "Smoother",
]

#: 允许输出的情绪标签（与前端 `EMOTION_EMOJI` 对齐）
EMOTIONS: tuple[str, ...] = (
    "neutral",
    "happy",
    "sad",
    "angry",
    "surprise",
    "fear",
    "disgust",
    "contempt",
    "pain",
)

#: 规则版本 —— 改了 :data:`RULES` 就升它，方便回头对日志
RULES_VERSION = 1

#: 每种情绪的"证据"：blendshape 名 → 权重（值越大越关键）。
#: blendshape 名取自 MediaPipe 官方 52 类（驼峰，如 ``mouthSmileLeft``）。
RULES: dict[str, dict[str, float]] = {
    "happy": {
        "mouthSmileLeft": 1.0, "mouthSmileRight": 1.0,
        "cheekSquintLeft": 0.5, "cheekSquintRight": 0.5,
    },
    "sad": {
        "mouthFrownLeft": 1.0, "mouthFrownRight": 1.0,
        "browInnerUp": 0.6, "mouthShrugLower": 0.3,
    },
    "angry": {
        "browDownLeft": 1.0, "browDownRight": 1.0,
        "eyeSquintLeft": 0.5, "eyeSquintRight": 0.5,
        "mouthPressLeft": 0.4, "mouthPressRight": 0.4,
    },
    "surprise": {
        "browOuterUpLeft": 0.8, "browOuterUpRight": 0.8,
        "eyeWideLeft": 0.6, "eyeWideRight": 0.6,
        "jawOpen": 0.9,
    },
    "fear": {
        "browInnerUp": 0.7, "eyeWideLeft": 0.5, "eyeWideRight": 0.5,
        "jawOpen": 0.4, "mouthStretchLeft": 0.4, "mouthStretchRight": 0.4,
    },
    "disgust": {
        "noseSneerLeft": 1.0, "noseSneerRight": 1.0,
        "mouthUpperUpLeft": 0.5, "mouthUpperUpRight": 0.5,
    },
    "contempt": {
        "mouthDimpleLeft": 0.5, "mouthDimpleRight": 0.5,
        "mouthSmileLeft": 0.4, "mouthSmileRight": 0.4,
        "eyeSquintLeft": 0.3, "eyeSquintRight": 0.3,
    },
    # 疼痛/难受：皱眉 + 眯眼 + 张嘴（和 angry 的区别主要在"眯眼"权重更高）
    "pain": {
        "eyeSquintLeft": 0.9, "eyeSquintRight": 0.9,
        "browDownLeft": 0.6, "browDownRight": 0.6,
        "jawOpen": 0.6, "mouthPressLeft": 0.4, "mouthPressRight": 0.4,
    },
}

#: 低于这个分就认为"没表情"（neutral），免得把无意识的微动读成情绪
DEFAULT_THRESHOLD = 0.35


def scores(blendshapes: Mapping[str, float] | None) -> dict[str, float]:
    """52 个 blendshape 分数 → 每种情绪的**加权平均分**（0~1）。

    用加权平均（而不是求和）是为了**不被"动作多"骗**：一个幅度很大的单一动作
    和一个幅度小但好几个协同动作的情绪，都能得到可比的分数。
    """
    out: dict[str, float] = {emo: 0.0 for emo in EMOTIONS}
    if not blendshapes:
        return out
    for emo, weights in RULES.items():
        total = 0.0
        for name, weight in weights.items():
            try:
                value = float(blendshapes.get(name, 0.0) or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            total += max(0.0, min(1.0, value)) * weight
        norm = sum(weights.values()) or 1.0
        out[emo] = round(total / norm, 4)
    return out


def classify(
    blendshapes: Mapping[str, float] | None,
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> tuple[str, float]:
    """→ ``(情绪标签, 置信度)``。

    所有情绪都低于 ``threshold`` → ``("neutral", 最高分)``。
    ``blendshapes`` 为空（没人脸 / 后端没给）→ ``("neutral", 0.0)``。
    """
    if not blendshapes:
        return "neutral", 0.0
    table = scores(blendshapes)
    label = max(table, key=lambda k: table[k])
    value = table[label]
    if value < threshold:
        return "neutral", value
    return label, value


def top_k(blendshapes: Mapping[str, float] | None, k: int = 3) -> list[dict[str, Any]]:
    """前 k 个情绪及分数（调试 / 面板想画柱状图时用）。"""
    table = scores(blendshapes)
    ordered = sorted(table.items(), key=lambda kv: kv[1], reverse=True)
    return [{"emotion": name, "score": score} for name, score in ordered[: max(1, k)]]


class Smoother:
    """情绪**时间平滑** —— 逐帧分类会抖，网页上会乱闪。

    做法：保留最近 ``window`` 次分类结果，取**众数**（并列时取分数高的那个）；
    置信度 = 众数占比 × 该标签在这段窗口里的平均分 —— 这样"3 帧 happy、2 帧 angry"
    不会给出虚高的置信度。

    :param window: 窗口大小（帧）。``1`` = 不平滑（等价于直接 :func:`classify`）。
    """

    def __init__(self, window: int = 7, *, threshold: float = DEFAULT_THRESHOLD) -> None:
        self.window = max(1, int(window))
        self.threshold = threshold
        self._hist: deque[tuple[str, float]] = deque(maxlen=self.window)

    def reset(self) -> None:
        """换摄像头 / 人脸消失时清空（别把上一个人的表情带过来）。"""
        self._hist.clear()

    def feed(self, blendshapes: Mapping[str, float] | None) -> tuple[str, float]:
        label, value = classify(blendshapes, threshold=self.threshold)
        self._hist.append((label, value))
        if self.window == 1:
            return label, value

        counts = Counter(lbl for lbl, _ in self._hist)
        best = max(counts, key=lambda lbl: (counts[lbl], sum(v for n, v in self._hist if n == lbl)))
        share = counts[best] / len(self._hist)
        avg = sum(v for n, v in self._hist if n == best) / counts[best]
        return best, round(share * avg, 4)
