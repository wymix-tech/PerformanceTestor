"""待迁移域的契约占位：golden 已捕获入库，域实现落地后逐个启用（阶段 5 步骤 2+）。

按 Issue #23 域顺序：stress → collector → datagen → source/ai → 其余。
每域迁移前先跑通对应 golden 的契约测试，再切流量（绞杀者模式纪律）。
"""

import pytest

from tests.contract._utils import load_golden

PENDING = {
    "auth_me": "auth 域（pyjwt）",
    "stress_tasks": "stress 域",
    "stress_task_detail": "stress 域",
    "stress_trend": "stress 域",
    "datagen_tasks": "datagen 域",
}


@pytest.mark.parametrize("name", sorted(PENDING))
def test_pending_domain_contract(name):
    pytest.skip(f"{PENDING[name]}尚未迁移；golden 已入库（{name}.json），"
                "域实现落地后替换本占位为真实结构断言")


def test_golden_files_complete():
    """golden 清单完整性：捕获脚本端点与入库文件一一对应。"""
    for name in ["auth_me", "stress_tasks", "stress_task_detail", "stress_trend",
                 "datagen_tasks", "ai_tools", "ai_knowledge"]:
        golden = load_golden(name)
        assert golden["status"] == 200, f"{name} 捕获时非 200，需在 Java 侧有数据环境重捕"
        assert "raw" in golden and "normalized" in golden
