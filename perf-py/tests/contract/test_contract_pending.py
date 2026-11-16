"""golden 清单完整性检查（阶段 5 · 步骤 5）。

捕获清单中的全部 golden 均已由各域契约测试对照使用（占位已清零）：
stress → test_contract_stress.py；datagen → test_contract_datagen.py；
ai → test_contract_ai.py；auth → test_contract_auth.py。
"""

import pytest

from tests.contract._utils import load_golden

GOLDEN_NAMES = ["auth_me", "stress_tasks", "stress_task_detail", "stress_trend",
                "datagen_tasks", "ai_tools", "ai_knowledge"]


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_golden_files_complete(name):
    """golden 清单完整性：捕获脚本端点与入库文件一一对应。"""
    golden = load_golden(name)
    assert golden["status"] == 200, f"{name} 捕获时非 200，需在 Java 侧有数据环境重捕"
    assert "raw" in golden and "normalized" in golden
