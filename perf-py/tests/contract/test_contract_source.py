"""source 域契约测试（阶段 5 · 步骤 5）：DB 读端点以 Java SourceController/实体/Mapper 源码为契约。

golden 未覆盖 source 域（捕获清单未含），沿用源码级契约先例；
重点断言：SourceRepo 的 gitToken @JsonIgnore 永不返回、各实体字段集合、
排序/过滤/limit 语义、缺失资源的 code=1 错误包装文案逐字对齐。
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from server.app import create_app
from server.collector_repo import SlowSqlModel, SourceRepoModel
from server.db import make_engine
from server.source_repo import (
    FixSuggestionModel,
    PerformanceIssueModel,
    SourceFileModel,
    SourceRepository,
)
from server.stress_repo import Base, StressRepository


def _seed(factory: sessionmaker) -> None:
    repos = [
        SourceRepoModel(repo_name="perf-demo", repo_type="local", source_path="/src/demo",
                        branch="main", app_name="demo-app", git_user="", git_token="secret-token",
                        status="ready", file_count=48, total_size=102400,
                        create_time=datetime(2026, 11, 13, 9, 0, 0)),
        SourceRepoModel(repo_name="perf-server", repo_type="git",
                        source_path="https://git.example.com/perf.git", branch="dev",
                        git_user="ci", git_token="git-token", status="ready",
                        file_count=246, total_size=204800,
                        create_time=datetime(2026, 11, 13, 9, 10, 0)),
    ]
    files = [
        SourceFileModel(repo_id=1, relative_path="src/main/java/DemoApp.java",
                        file_name="DemoApp.java", language="java", size=1024,
                        content_hash="abc", create_time=datetime(2026, 11, 13, 9, 1, 0)),
        SourceFileModel(repo_id=1, relative_path="src/main/java/Order.java",
                        file_name="Order.java", language="java", size=2048,
                        content_hash="def", create_time=datetime(2026, 11, 13, 9, 1, 0)),
    ]
    issues = [
        PerformanceIssueModel(issue_type="slow_sql", app_name="demo-app",
                              title="perf_order 全表扫描", evidence_json='{"rows":100000}',
                              source_repo_id=1, source_file_id=1, line_start=10,
                              line_end=20, root_cause="缺索引", status="open",
                              stress_task_id=3,
                              create_time=datetime(2026, 11, 13, 10, 0, 0)),
        PerformanceIssueModel(issue_type="hotspot", app_name="demo-app",
                              title="热点方法 CPU 占比高", status="resolved",
                              create_time=datetime(2026, 11, 13, 11, 0, 0)),
    ]
    suggestions = [
        FixSuggestionModel(issue_id=1, title="补复合索引", files_json='[{"file":"OrderMapper.xml"}]',
                           changes_json='[{"point":"增加 idx_user_status"}]', benefit="P95 -80%",
                           risk="写入放大", selected=1,
                           create_time=datetime(2026, 11, 13, 10, 5, 0)),
        FixSuggestionModel(issue_id=1, title="改写 SQL 走覆盖索引",
                           selected=0, create_time=datetime(2026, 11, 13, 10, 6, 0)),
    ]
    now = datetime.now()
    slow_sqls = [
        SlowSqlModel(app_name="demo-app", sql_text="SELECT * FROM perf_order",
                     cost_ms=1500.0, source="jdbc", db_host="10.0.0.20",
                     method_chain='["A","B"]', execute_time=now - timedelta(hours=1)),
        SlowSqlModel(app_name="demo-app", sql_text="SELECT * FROM perf_user",
                     cost_ms=900.0, source="slowlog", db_host="10.0.0.20",
                     execute_time=now - timedelta(days=8)),  # 超出 7 天窗口 → 不返回
    ]
    with factory() as s:
        s.add_all(repos + files + issues + suggestions + slow_sqls)
        s.commit()


@pytest.fixture()
def client() -> TestClient:
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    Base.metadata.create_all(factory.kw["bind"])
    _seed(factory)
    app = create_app(stress_repo=StressRepository(factory),
                     source_repo=SourceRepository(factory), auth_enabled=False)
    return TestClient(app)


def test_repos_git_token_never_returned(client):
    """/api/source/repos：gitToken @JsonIgnore 永不返回（安全契约），其余 12 字段对齐。"""
    body = client.get("/api/source/repos", params={"limit": 50}).json()
    assert body["code"] == 0
    rows = body["data"]
    assert [r["repoName"] for r in rows] == ["perf-server", "perf-demo"], "create_time 倒序"
    assert set(rows[0]) == {"id", "repoName", "repoType", "sourcePath", "branch",
                            "appName", "gitUser", "status", "fileCount",
                            "totalSize", "errorMsg", "createTime"}
    assert all("gitToken" not in r for r in rows), "git 凭据永不返回"
    assert rows[0]["fileCount"] == 246 and rows[0]["errorMsg"] is None


def test_files_prefix_filter_and_ordering(client):
    """files：relative_path 前缀过滤 + 升序；元素字段对齐 SourceFile 实体（8 字段）。"""
    body = client.get("/api/source/repos/1/files").json()
    assert [f["fileName"] for f in body["data"]] == ["DemoApp.java", "Order.java"]
    assert set(body["data"][0]) == {"id", "repoId", "relativePath", "fileName",
                                    "language", "size", "contentHash", "createTime"}
    body = client.get("/api/source/repos/1/files", params={"prefix": "src/main/java/Order"}).json()
    assert [f["fileName"] for f in body["data"]] == ["Order.java"]


def test_issues_filter_and_element_contract(client):
    """issues：appName/issueType/status 过滤 + create_time 倒序；元素对齐实体（15 字段）。"""
    body = client.get("/api/source/issues", params={"appName": "demo-app"}).json()
    rows = body["data"]
    assert [r["title"] for r in rows] == ["热点方法 CPU 占比高", "perf_order 全表扫描"]
    assert set(rows[0]) == {"id", "issueType", "appName", "title", "evidenceJson",
                            "sourceRepoId", "sourceFileId", "lineStart", "lineEnd",
                            "rootCause", "status", "stressTaskId", "verifyJson",
                            "verifyTime", "createTime"}
    assert rows[0]["verifyTime"] is None and rows[1]["stressTaskId"] == 3
    body = client.get("/api/source/issues", params={"status": "open"}).json()
    assert [r["status"] for r in body["data"]] == ["open"]


def test_issue_detail_with_suggestions(client):
    """issue detail：{issue, suggestions[]}（按 id 升序）；缺失 → code=1「问题不存在: {id}」。"""
    body = client.get("/api/source/issues/1").json()
    assert body["code"] == 0
    assert set(body["data"]) == {"issue", "suggestions"}
    assert [s["title"] for s in body["data"]["suggestions"]] == ["补复合索引", "改写 SQL 走覆盖索引"]
    assert set(body["data"]["suggestions"][0]) == {
        "id", "issueId", "title", "filesJson", "changesJson", "codeChangesJson",
        "benefit", "risk", "promptMd", "selected", "createTime"}
    missing = client.get("/api/source/issues/999")
    assert missing.status_code == 200
    assert missing.json() == {"code": 1, "message": "问题不存在: 999", "data": None}


def test_recent_slow_sqls_window_and_ordering(client):
    """recent-slow-sqls：仅近 7 天 + cost_ms 倒序 + app 过滤；元素对齐 SlowSql 实体（8 字段）。"""
    body = client.get("/api/source/recent-slow-sqls", params={"app": "demo-app"}).json()
    rows = body["data"]
    assert [r["costMs"] for r in rows] == [1500.0], "8 天前的记录不在 7 天窗口内"
    assert set(rows[0]) == {"id", "appName", "sqlText", "costMs", "source",
                            "dbHost", "methodChain", "executeTime"}
    assert rows[0]["methodChain"] == '["A","B"]'


def test_suggestion_detail_and_missing_message(client):
    """suggestion 详情；缺失 → code=1「方案不存在: {id}」逐字对齐。"""
    body = client.get("/api/source/suggestions/1").json()
    assert body["data"]["title"] == "补复合索引" and body["data"]["selected"] == 1
    missing = client.get("/api/source/suggestions/999")
    assert missing.json() == {"code": 1, "message": "方案不存在: 999", "data": None}
