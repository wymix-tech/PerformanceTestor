"""datagen 域契约测试（阶段 5 · 步骤 4）：Python server 读路径对照 Java golden / 源码契约。

- datagen_tasks：对照 golden（空库捕获，data=[]）；
- 元素结构 / suggestions / plans：golden 未捕获元素结构（捕获时空库），
  以 Java DataGenTask / SemanticHint / DataGenPlan 实体与 Mapper 源码为契约
  （沿用 ai_knowledge 空库 golden 的源码级契约先例）。
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from server.app import create_app
from server.datagen_repo import (
    DataGenPlanModel,
    DatagenRepository,
    DataGenTaskModel,
    SemanticHintModel,
)
from server.db import make_engine
from server.stress_repo import Base, StressRepository
from tests.contract._utils import assert_matches_golden, load_golden


def _seed(factory: sessionmaker) -> None:
    hints = [
        SemanticHintModel(repo_id=1, hint_type="value", table_name="perf_order",
                          column_name="status", values_json='["SHIPPED","PAID"]',
                          source="literal", confidence=0.9, status="pending",
                          create_time=datetime(2026, 11, 13, 10, 0, 0)),
        SemanticHintModel(repo_id=1, hint_type="relation", table_name="perf_order",
                          column_name="user_id", parent_table="perf_user",
                          parent_column="id", source="orm_join", confidence=0.7,
                          status="pending", create_time=datetime(2026, 11, 13, 10, 1, 0)),
        SemanticHintModel(repo_id=2, hint_type="value", table_name="other_t",
                          column_name="kind", values_json='["A"]',
                          source="enum", confidence=0.5, status="dismissed",
                          create_time=datetime(2026, 11, 13, 10, 2, 0)),
    ]
    tasks = [
        DataGenTaskModel(task_name="perf_order 灌数", db_url="jdbc:mysql://127.0.0.1/perf",
                         table_name="perf_order", row_count=10000, status="finished",
                         inserted=10000, total_rows=10000, cost_ms=3210,
                         create_time=datetime(2026, 11, 13, 11, 0, 0)),
        DataGenTaskModel(task_name="perf_user 灌数", db_url="jdbc:mysql://127.0.0.1/perf",
                         table_name="perf_user", row_count=2000, status="failed",
                         error_msg="connection refused",
                         create_time=datetime(2026, 11, 13, 12, 0, 0)),
    ]
    plans = [
        DataGenPlanModel(repo_id=1, plan_name="POST /api/order/list",
                         endpoint_json='[{"httpMethod":"POST","path":"/api/order/list","weight":100}]',
                         conn_id=1, status="adopted",
                         create_time=datetime(2026, 11, 13, 9, 0, 0),
                         update_time=datetime(2026, 11, 13, 9, 5, 0)),
        DataGenPlanModel(repo_id=1, plan_name="GET /api/user/list",
                         status="pending",
                         create_time=datetime(2026, 11, 13, 9, 30, 0),
                         update_time=datetime(2026, 11, 13, 9, 30, 0)),
        DataGenPlanModel(repo_id=2, plan_name="other repo plan", status="discarded",
                         create_time=datetime(2026, 11, 13, 9, 40, 0),
                         update_time=datetime(2026, 11, 13, 9, 40, 0)),
    ]
    with factory() as s:
        s.add_all(hints + tasks + plans)
        s.commit()


@pytest.fixture()
def client() -> TestClient:
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    Base.metadata.create_all(factory.kw["bind"])
    _seed(factory)
    app = create_app(stress_repo=StressRepository(factory),
                     datagen_repo=DatagenRepository(factory))
    return TestClient(app)


def test_tasks_empty_matches_golden(client):
    """/api/datagen/tasks?limit=5：golden 为空库捕获（data=[]），ApiResponse 包装一致。"""
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"))
    Base.metadata.create_all(factory.kw["bind"])
    empty_app = create_app(stress_repo=StressRepository(factory),
                           datagen_repo=DatagenRepository(factory))
    resp = TestClient(empty_app).get("/api/datagen/tasks", params={"limit": 5})
    assert_matches_golden(resp.json(), load_golden("datagen_tasks"), resp.status_code)


def test_tasks_element_matches_java_entity(client):
    """任务元素结构对齐 Java DataGenTask 实体（14 字段 camelCase，源码级契约）。"""
    body = client.get("/api/datagen/tasks", params={"limit": 5}).json()
    assert body["code"] == 0
    rows = body["data"]
    assert [r["taskName"] for r in rows] == ["perf_user 灌数", "perf_order 灌数"], \
        "create_time 倒序"
    assert set(rows[0]) == {"id", "taskName", "dbUrl", "tableName", "rowCount",
                            "ruleJson", "status", "inserted", "totalRows",
                            "tablesJson", "detailJson", "costMs", "errorMsg",
                            "createTime"}
    assert rows[1]["errorMsg"] is None and rows[0]["errorMsg"] == "connection refused"
    assert isinstance(rows[0]["createTime"], str)


def test_detail_missing_returns_null_data(client):
    """详情任务不存在：data=null（findById 空返回 → ApiResponse.ok(null)）。"""
    body = client.get("/api/datagen/tasks/999").json()
    assert body["code"] == 0 and body["data"] is None


def test_suggestions_filters_and_ordering(client):
    """suggestions：confidence DESC, id DESC；repoId/tableName/hintType/status 过滤。"""
    body = client.get("/api/datagen/suggestions",
                      params={"repoId": 1, "status": "pending"}).json()
    rows = body["data"]
    assert [r["hintType"] for r in rows] == ["value", "relation"], "confidence 倒序"
    assert set(rows[0]) == {"id", "repoId", "hintType", "tableName", "columnName",
                            "valuesJson", "parentTable", "parentColumn",
                            "sourceRefs", "source", "confidence", "status",
                            "createTime"}, "对齐 Java SemanticHint 实体（13 字段）"
    relation = rows[1]
    assert relation["parentTable"] == "perf_user" and relation["parentColumn"] == "id"

    # tableName 过滤 + 无过滤（repoId=null 的行也可能出现）
    body = client.get("/api/datagen/suggestions", params={"tableName": "other_t"}).json()
    assert [r["tableName"] for r in body["data"]] == ["other_t"]
    assert body["data"][0]["status"] == "dismissed"


def test_plans_filters_and_ordering(client):
    """plans：create_time DESC, id DESC（同秒按 id 降序）；repoId/status 过滤。"""
    body = client.get("/api/datagen/plans", params={"repoId": 1}).json()
    rows = body["data"]
    assert [r["planName"] for r in rows] == ["GET /api/user/list", "POST /api/order/list"]
    assert set(rows[0]) == {"id", "repoId", "planName", "endpointJson", "stressJson",
                            "connId", "dbUrl", "tablesJson", "warningsJson", "status",
                            "taskId", "createTime", "updateTime"}, \
        "对齐 Java DataGenPlan 实体（13 字段）"
    assert rows[1]["endpointJson"] is not None and rows[0]["endpointJson"] is None

    body = client.get("/api/datagen/plans", params={"status": "discarded"}).json()
    assert [r["status"] for r in body["data"]] == ["discarded"]


def test_plan_detail_and_limit_semantics(client):
    """方案详情 + limit 截断（1..200，对齐 planService.list）。"""
    body = client.get("/api/datagen/plans/1").json()
    assert body["data"]["planName"] == "POST /api/order/list"
    assert client.get("/api/datagen/plans/999").json()["data"] is None
    assert len(client.get("/api/datagen/plans", params={"limit": 2}).json()["data"]) == 2
