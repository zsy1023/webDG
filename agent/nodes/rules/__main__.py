"""``python -m agent.nodes.rules`` 入口。

规则来源：``--rules-file`` 指定的 JSON，或 ``data/rules.json``（存在时自动读）。

    python -m agent.nodes.rules
    python -m agent.core.cli run rules
"""

from __future__ import annotations

from agent.nodes.rules.node import RulesNode

if __name__ == "__main__":
    raise SystemExit(RulesNode.main())
