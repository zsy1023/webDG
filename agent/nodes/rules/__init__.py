"""``rules`` —— 判定引擎节点（T8，控制端判定 → ``command/fire``）。

对外形态：:class:`agent.nodes.rules.node.RulesNode`。

**条件体系**（全在 :mod:`.condition`）—— 含协调者要求的"缓冲 + 综合判定"：

===============  ============================================================
原子              ``left_wrist:y>0.5`` / ``rect:nose,0.2,0.3,0.5,0.6`` /
                  ``circle:left_wrist,0.5,0.5,0.08`` / ``>120``（心率）/ ``A``（按钮）
组合              ``a && b`` 都满足（**综合判定**）| ``a || b`` 任一满足
                  ⚠️ 不许混用 ``&&`` 与 ``||``
维持              ``<条件>@<秒>`` —— 保持满算通过，中途**被破坏**才罚
缓冲              ``Rule.debounce_ms``：条件要**连续成立**这么久才算数（默认 250ms）
可信              ``Rule.min_visibility``：关键点可见度不足就**不参与**判定（默认 0.5）
===============  ============================================================
"""

from __future__ import annotations

from agent.nodes.rules.condition import (
    DEFAULT_HOLD_MS,
    DEFAULT_MIN_VISIBILITY,
    LANDMARK_INDEX,
    LANDMARKS,
    Atom,
    Condition,
    Debouncer,
    HoldCondition,
    RuleError,
    Sample,
    button,
    guard_line,
    heart_threshold,
    hold_pose,
    parse_atoms,
    parse_condition,
)
from agent.nodes.rules.node import Rule, RuleRuntime, RulesNode

__all__ = [
    "RulesNode",
    "Rule",
    "RuleRuntime",
    "RuleError",
    "Sample",
    "Atom",
    "Condition",
    "HoldCondition",
    "Debouncer",
    "parse_atoms",
    "parse_condition",
    "guard_line",
    "hold_pose",
    "button",
    "heart_threshold",
    "LANDMARKS",
    "LANDMARK_INDEX",
    "DEFAULT_HOLD_MS",
    "DEFAULT_MIN_VISIBILITY",
]
