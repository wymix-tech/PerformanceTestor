"""AI 域契约测试：Python server 响应结构对照 Java golden（阶段 5 · 步骤 1）。

golden 由 scripts/capture-contract.sh 对运行中的 Java perf-server 捕获。
比对规则：normalize 后相等（键集合与类型一致；标量值不比对）。
"""

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.knowledge import KnowledgeService, SqliteKnowledgeStore
from tests.contract._utils import assert_matches_golden, load_golden


@pytest.fixture()
def client() -> TestClient:
    app = create_app(llm=None, knowledge=KnowledgeService(SqliteKnowledgeStore()))
    return TestClient(app)


def test_tools_contract_matches_java(client):
    """/api/ai/tools：ApiResponse 包装 + [{name, description}] 元素形状。

    工具集合内容允许两侧不同（能力清单各自演进），契约只锁定元素结构。
    """
    golden = load_golden("ai_tools")
    resp = client.get("/api/ai/tools")
    assert_matches_golden(resp.json(), golden, resp.status_code)
    # 元素键集合与 Java 侧完全一致（golden 数组取首元素）
    assert set(golden["normalized"]["data"][0]) == {"name", "description"}


def test_knowledge_contract_matches_java(client):
    """/api/ai/knowledge：ApiResponse 包装 + 空结果为 []（无 recent 回落语义同 Java）。"""
    golden = load_golden("ai_knowledge")
    resp = client.get("/api/ai/knowledge", params={"limit": 5})
    assert_matches_golden(resp.json(), golden, resp.status_code)


def test_knowledge_item_shape_matches_java_entity():
    """知识条目字段集合对齐 Java AiKnowledge 实体（源码级契约，
    golden 空库未捕获元素结构，以 perf-server AiKnowledge.java 字段为准）。
    """
    from server.knowledge import KnowledgeItem

    item = KnowledgeItem()
    assert set(item.to_camel_json()) == {
        "id", "appName", "title", "content", "tags", "source", "createTime", "score",
    }, "Java AiKnowledge.java 字段（camelCase Jackson 序列化）"


def test_knowledge_contract_with_data(client):
    """有数据时元素结构仍与 Java 实体契约一致（捕获脚本未覆盖，源码契约补充）。"""
    app = client.app
    app.state.knowledge.save(
        "demo", None,
        "结论：订单接口 P95 恶化根因为 perf_order 表 user_id 列缺少索引，"
        "联表扫描行数达百万级，建议补充复合索引后回归压测验证收益。",
        "analysis",
    )
    resp = client.get("/api/ai/knowledge", params={"query": "索引", "app": "demo"})
    body = resp.json()
    assert body["code"] == 0 and len(body["data"]) == 1
    item = body["data"][0]
    assert set(item) == {"id", "appName", "title", "content", "tags",
                         "source", "createTime", "score"}
