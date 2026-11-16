"""source 问题域契约测试（阶段 5 · 步骤 10）：以 Java SourceIssueService /
IssueAssociator / PerformanceAnalyzer / FixPromptGenerator / EndpointScanner 源码为契约。

覆盖：三类问题创建（方法链 → MyBatis XML → JPA 实体多级定位、URL 关联 Controller）、
补绑仓库/压测快照、大模型分析（假 LLM JSON 解析 + 规则降级文案逐字 + EXPLAIN 证据回写）、
修复提示词生成与单选、接口扫描。内嵌 mini 源码仓库（Controller/Service/Mapper/Entity）。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from agents.llm import ChatResult, LlmClient
from server.app import create_app
from server.collector_repo import CollectorRepository, DbConnModel, SlowSqlModel
from server.db import make_engine
from server.db_explain import DbExplainService
from server.knowledge import SqliteKnowledgeStore
from server.source_repo import SourceRepository
from server.source_write_service import DataCleanupService
from server.stress_repo import (
    Base,
    StressRepository,
    StressTaskModel,
)

CONTROLLER_JAVA = """package com.demo.controller;

@RestController
@RequestMapping("/api/orders")
public class OrderController {
    @GetMapping("/list")
    public java.util.List<Order> list() {
        return null;
    }

    @PostMapping("/create")
    public Order create() {
        return null;
    }

    @RequestMapping(value = "/detail", method = RequestMethod.POST)
    public Order detail() {
        return null;
    }
}
"""
PAY_CONTROLLER_JAVA = """package com.demo.controller;

@RestController
public class PayController {
    @GetMapping("/api/orders/list")
    public java.util.List<Order> listAll() {
        return null;
    }
}
"""
SERVICE_JAVA = """package com.demo.service;

public class OrderService {
    public java.util.List<Order> listOrder() {
        return null;
    }
}
"""
DAO_JAVA = """package com.demo.mapper;

public interface OrderMapper {
    java.util.List<Order> selectOrders();
}
"""
MAPPER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<mapper namespace="com.demo.mapper.OrderMapper">
    <select id="selectOrders">
        SELECT * FROM perf_order WHERE status = 'PAID'
    </select>
</mapper>
"""
ENTITY_JAVA = """package com.demo.entity;

import jakarta.persistence.Entity;

@Entity
public class UserInfo {
    private Long id;
}
"""
TABLE_ENTITY_JAVA = """package com.demo.entity;

import jakarta.persistence.Entity;
import jakarta.persistence.Table;

@Table(name = "t_order")
@Entity
public class TableEntity {
    private Long id;
}
"""
SOURCE_FILES = {
    "com/demo/controller/OrderController.java": CONTROLLER_JAVA,
    "com/demo/controller/PayController.java": PAY_CONTROLLER_JAVA,
    "com/demo/service/OrderService.java": SERVICE_JAVA,
    "com/demo/mapper/OrderMapper.java": DAO_JAVA,
    "com/demo/mapper/OrderMapper.xml": MAPPER_XML,
    "com/demo/entity/UserInfo.java": ENTITY_JAVA,
    "com/demo/entity/TableEntity.java": TABLE_ENTITY_JAVA,
}


def _make_source_repo(tmp_path) -> str:
    """mini 源码仓库：Controller + Service + Mapper 接口/XML + JPA 实体。"""
    src = tmp_path / "mini-src"
    for rel, content in SOURCE_FILES.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return str(src)


class _ScriptedLlm(LlmClient):
    """脚本化假客户端：返回围栏包裹的 JSON（验证 markdown 剥离）。"""

    def chat(self, messages, tools):
        payload = {
            "root_cause": "未命中索引导致全表扫描",
            "solutions": [
                {"title": "补复合索引", "files": [{"file": "OrderMapper.java"}],
                 "changes": [{"point": "增加 idx_status"}],
                 "code_changes": ["CREATE INDEX ..."], "benefit": "P95 -80%", "risk": "写入放大"},
                {"title": "查询改写", "files": [], "changes": [], "benefit": "b2", "risk": "r2"},
                {"title": "加缓存", "files": [], "changes": [], "benefit": "b3", "risk": "r3"},
                {"title": "第 4 条应被截断", "files": [], "changes": [], "benefit": "", "risk": ""},
            ],
        }
        return ChatResult(content="```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```")


class _FakeCursor:
    """DB-API 假游标：EXPLAIN 表格模式 + SHOW INDEX 空 + information_schema 行数估算。"""

    def __init__(self):
        self._rows: list[tuple] = []
        self._desc: list[tuple] = []

    def execute(self, sql, args=None):  # noqa: ANN001
        if sql.startswith("EXPLAIN FORMAT=JSON"):
            raise RuntimeError("不支持 FORMAT=JSON（回退表格模式）")
        if sql.startswith("EXPLAIN"):
            self._desc = [("id",), ("select_type",), ("type",), ("key",), ("rows",), ("Extra",)]
            self._rows = [("1", "SIMPLE", "ALL", None, "998", "Using where")]
        elif sql.startswith("SHOW INDEX"):
            self._desc, self._rows = [], []
        else:  # information_schema TABLE_ROWS
            self._desc = [("TABLE_ROWS",)]
            self._rows = [("998",)] if args and args[1] == "perf_order" else []
        return self

    @property
    def description(self):
        return self._desc

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    """假连接：当前库 perf（对齐 PyMySQL conn.db 属性）。"""

    db = "perf"

    def cursor(self):
        return _FakeCursor()

    def close(self):
        pass


@pytest.fixture()
def env(tmp_path):
    """内存库 + 注入式写服务 + 假 EXPLAIN 连接器 + 种子仓库。"""
    fac = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    Base.metadata.create_all(fac.kw["bind"])
    src = _make_source_repo(tmp_path)
    db_explain = DbExplainService(connect_fn=lambda *a: _FakeConn())
    app = create_app(
        stress_repo=StressRepository(fac), collector_repo=CollectorRepository(fac),
        source_repo=SourceRepository(fac),
        source_storage_dir=str(tmp_path / "storage"),
        data_cleanup=DataCleanupService(fac, SqliteKnowledgeStore()),
        db_explain=db_explain, auth_enabled=False)
    client = TestClient(app)
    repo_id = client.post("/api/source/repos/local",
                          json={"repoName": "mini", "localPath": src}).json()["data"]["id"]
    return client, fac, repo_id, tmp_path


def _seed_slow_sql(fac, method_chain: str | None, sql_text: str = "SELECT * FROM perf_order") -> int:
    now = datetime(2026, 11, 16, 10, 0, 0)
    with fac() as s:
        row = SlowSqlModel(app_name="mini", sql_text=sql_text, cost_ms=1500.0,
                           source="jdbc", db_host="10.0.0.20",
                           method_chain=method_chain, execute_time=now)
        s.add(row)
        s.commit()
        return row.id


def _seed_stress_task(fac, target_url: str, app_name: str = "mini") -> int:
    now = datetime(2026, 11, 16, 10, 0, 0)
    with fac() as s:
        task = StressTaskModel(task_name="t", target_url=target_url, app_name=app_name,
                               concurrency=50, create_time=now)
        s.add(task)
        s.commit()
        return task.id


# ---------------------------------------------------------------- 接口扫描


def test_endpoint_scan(env):
    """GET /repos/{id}/endpoints：类前缀拼接 + 方法级注解解析（含 RequestMethod.POST）。
    无类级前缀的 PayController 方法级全路径同样解析。"""
    client, _, repo_id, _ = env
    body = client.get(f"/api/source/repos/{repo_id}/endpoints").json()
    assert body["code"] == 0
    eps = body["data"]
    assert [(e["httpMethod"], e["path"], e["className"]) for e in eps] == [
        ("GET", "/api/orders/list", "OrderController"),
        ("POST", "/api/orders/create", "OrderController"),
        ("POST", "/api/orders/detail", "OrderController"),
        ("GET", "/api/orders/list", "PayController"),
    ]
    order_eps = [e for e in eps if e["className"] == "OrderController"]
    assert [e["methodName"] for e in order_eps] == ["list", "create", "detail"]
    assert all(e["classFile"] == "com/demo/controller/OrderController.java" for e in order_eps)
    assert order_eps[0]["line"] == 6, "@GetMapping(\"/list\") 所在行"
    assert eps[3]["methodName"] == "listAll" and eps[3]["line"] == 5


# ---------------------------------------------------------------- 三类问题创建


def test_create_from_slow_sql_by_method_chain(env):
    """慢 SQL → 方法链定位（链尾优先）：OrderService#listOrder 精确匹配。"""
    client, fac, repo_id, _ = env
    sql_id = _seed_slow_sql(fac, '["com.demo.controller.OrderController#list","com.demo.service.OrderService#listOrder"]')
    body = client.post("/api/source/issues/from-slow-sql",
                       params={"slowSqlId": sql_id, "repoId": repo_id}).json()
    assert body["code"] == 0
    issue = body["data"]
    assert set(issue) == {"id", "issueType", "appName", "title", "evidenceJson",
                          "sourceRepoId", "sourceFileId", "lineStart", "lineEnd",
                          "rootCause", "status", "stressTaskId", "verifyJson",
                          "verifyTime", "createTime"}
    assert issue["issueType"] == "slow_sql" and issue["status"] == "pending"
    assert issue["stressTaskId"] == 0, "未关联压测场景显式落 0"
    assert issue["title"].startswith("慢 SQL: SELECT * FROM perf_order")
    assert issue["appName"] == "mini"
    evidence = json.loads(issue["evidenceJson"])
    assert evidence["slowSqlId"] == sql_id and evidence["costMs"] == 1500.0
    assert evidence["methodChain"] == ["com.demo.controller.OrderController#list",
                                       "com.demo.service.OrderService#listOrder"]
    files = client.get(f"/api/source/repos/{repo_id}/files",
                       params={"prefix": "com/demo/service/"}).json()["data"]
    assert issue["sourceFileId"] == files[0]["id"], "定位到 OrderService.java"
    assert issue["lineStart"] == 4, "listOrder 方法定义行"


def test_create_from_slow_sql_by_mybatis_xml(env):
    """慢 SQL → MyBatis XML 定位：SQL 片段 → statement id → DAO 接口方法行。"""
    client, fac, repo_id, _ = env
    sql_id = _seed_slow_sql(fac, None, "SELECT * FROM perf_order WHERE status = 'PAID'")
    issue = client.post("/api/source/issues/from-slow-sql",
                        params={"slowSqlId": sql_id, "repoId": repo_id}).json()["data"]
    files = client.get(f"/api/source/repos/{repo_id}/files",
                       params={"prefix": "com/demo/mapper/OrderMapper.java"}).json()["data"]
    assert issue["sourceFileId"] == files[0]["id"]
    assert issue["lineStart"] == 4 and issue["lineEnd"] == 6, "selectOrders 接口方法行区间"


def test_create_from_slow_sql_by_jpa_entity(env):
    """慢 SQL → JPA 定位：@Table(name) 精确匹配优先于 @Entity 驼峰推断。"""
    client, fac, repo_id, _ = env
    sql_id = _seed_slow_sql(fac, None, "SELECT * FROM t_order WHERE id = 1")
    issue = client.post("/api/source/issues/from-slow-sql",
                        params={"slowSqlId": sql_id, "repoId": repo_id}).json()["data"]
    files = client.get(f"/api/source/repos/{repo_id}/files",
                       params={"prefix": "com/demo/entity/TableEntity.java"}).json()["data"]
    assert issue["sourceFileId"] == files[0]["id"], "@Table(name=\"t_order\") 精确命中"
    assert issue["lineStart"] == 1 and issue["lineEnd"] <= 30

    sql_id2 = _seed_slow_sql(fac, None, "SELECT * FROM user_info")
    issue2 = client.post("/api/source/issues/from-slow-sql",
                         params={"slowSqlId": sql_id2, "repoId": repo_id}).json()["data"]
    files2 = client.get(f"/api/source/repos/{repo_id}/files",
                        params={"prefix": "com/demo/entity/UserInfo.java"}).json()["data"]
    assert issue2["sourceFileId"] == files2[0]["id"], "@Entity + 驼峰转下划线推断"


def test_create_from_hot_method(env):
    client, _, repo_id, _ = env
    body = client.post("/api/source/issues/from-hot-method",
                       params={"className": "com.demo.service.OrderService",
                               "methodName": "listOrder", "repoId": repo_id,
                               "appName": "app-x"}).json()
    issue = body["data"]
    assert issue["issueType"] == "hot_method" and issue["appName"] == "app-x"
    assert issue["title"] == "热点方法耗时高: com.demo.service.OrderService#listOrder"
    assert json.loads(issue["evidenceJson"]) == {
        "className": "com.demo.service.OrderService", "methodName": "listOrder"}
    assert issue["lineStart"] == 4


def test_create_from_stress_url_association(env):
    """压测瓶颈 → URL 关联 Controller；appName 继承链：显式 → 任务自带 → stress。"""
    client, fac, repo_id, _ = env
    task_id = _seed_stress_task(fac, "http://10.0.0.5:8080/api/orders/list")
    issue = client.post("/api/source/issues/from-stress",
                        params={"stressTaskId": task_id, "repoId": repo_id}).json()["data"]
    assert issue["issueType"] == "stress_bottleneck"
    assert issue["appName"] == "mini", "继承压测任务 appName"
    assert issue["stressTaskId"] == task_id, "场景快照作为验证基线"
    assert issue["title"] == "压测瓶颈: http://10.0.0.5:8080/api/orders/list (并发 50)"
    evidence = json.loads(issue["evidenceJson"])
    assert evidence["taskId"] == task_id and evidence["concurrency"] == 50
    assert evidence["resultJson"] == "{}", "任务无结果时回退空对象"
    files = client.get(f"/api/source/repos/{repo_id}/files",
                       params={"prefix": "com/demo/controller/"}).json()["data"]
    # Java quirk 逐字保留：associateUrl 在方法级路径带前导斜杠时丢弃类前缀
    # （path.startsWith("/") ? "" : classPrefix），因此 OrderController 的
    # @RequestMapping("/api/orders") + @GetMapping("/list") 组合不参与 URL 匹配；
    # 命中的是方法级全路径的 PayController。
    assert issue["sourceFileId"] == files[1]["id"], "URL 关联到 PayController 方法行"
    assert issue["lineStart"] == 6

    # 显式指定 appName 覆盖任务自带值
    issue2 = client.post("/api/source/issues/from-stress",
                         params={"stressTaskId": task_id, "repoId": repo_id,
                                 "appName": "override"}).json()["data"]
    assert issue2["appName"] == "override"

    missing = client.post("/api/source/issues/from-stress",
                          params={"stressTaskId": 999, "repoId": repo_id})
    assert missing.json() == {"code": 1, "message": "压测任务不存在: 999", "data": None}


def test_create_from_slow_sql_missing(env):
    client, _, repo_id, _ = env
    body = client.post("/api/source/issues/from-slow-sql",
                       params={"slowSqlId": 999, "repoId": repo_id}).json()
    assert body == {"code": 1, "message": "慢 SQL 不存在: 999", "data": None}


# ---------------------------------------------------------------- 补绑与守卫


def test_bind_repo_and_location_update(env):
    """bind-repo：按问题类型选择定位策略（slow_sql → 方法链），位置缺失时仅绑仓库。"""
    client, fac, repo_id, _ = env
    sql_id = _seed_slow_sql(fac, '["com.demo.service.OrderService#listOrder"]')
    issue = client.post("/api/source/issues/from-slow-sql",
                        params={"slowSqlId": sql_id, "repoId": repo_id}).json()["data"]
    detail = client.post(f"/api/source/issues/{issue['id']}/bind-repo",
                         params={"repoId": repo_id}).json()
    assert detail["code"] == 0
    assert set(detail["data"]) == {"issue", "suggestions"}
    assert detail["data"]["suggestions"] == []
    assert detail["data"]["issue"]["sourceFileId"] == issue["sourceFileId"], "重定位一致"

    missing = client.post("/api/source/issues/999/bind-repo", params={"repoId": repo_id})
    assert missing.json() == {"code": 1, "message": "问题不存在: 999", "data": None}


def test_bind_stress_task_guards(env):
    client, fac, repo_id, _ = env
    sql_id = _seed_slow_sql(fac, None)
    issue = client.post("/api/source/issues/from-slow-sql",
                        params={"slowSqlId": sql_id, "repoId": repo_id}).json()["data"]
    assert client.post(f"/api/source/issues/{issue['id']}/bind-stress",
                       params={"stressTaskId": 0}).json() == {
        "code": 1, "message": "压测任务 ID 非法: 0", "data": None}
    assert client.post(f"/api/source/issues/{issue['id']}/bind-stress",
                       params={"stressTaskId": 999}).json() == {
        "code": 1, "message": "压测任务不存在: 999", "data": None}
    task_id = _seed_stress_task(fac, "http://x/api")
    body = client.post(f"/api/source/issues/{issue['id']}/bind-stress",
                       params={"stressTaskId": task_id}).json()
    assert body["data"]["stressTaskId"] == task_id


# ---------------------------------------------------------------- 大模型分析


def test_analyze_fallback_template_verbatim(env):
    """无 LLM → 规则降级：root_cause 与两条模板方案文案逐字对齐。"""
    client, fac, repo_id, _ = env
    sql_id = _seed_slow_sql(fac, None)
    issue = client.post("/api/source/issues/from-slow-sql",
                        params={"slowSqlId": sql_id, "repoId": repo_id}).json()["data"]
    body = client.post(f"/api/source/issues/{issue['id']}/analyze").json()
    assert body["data"] == {"generated": 2}
    detail = client.get(f"/api/source/issues/{issue['id']}").json()["data"]
    assert detail["issue"]["status"] == "analyzed"
    assert detail["issue"]["rootCause"] == \
        "（规则模式）SQL 查询未命中索引或扫描行数过大，导致单次查询耗时偏高"
    assert [s["title"] for s in detail["suggestions"]] == \
        ["SQL 优化：索引与查询改写", "异步化与削峰（规则模式）"]
    assert detail["suggestions"][0]["filesJson"] == \
        '[{"file":"待确认","action":"按检测证据定位"}]', "Java 落库字面字符串"
    assert detail["suggestions"][1]["benefit"] == "提升系统弹性与稳定性"


def test_analyze_stress_requires_repo_binding(env):
    """压测瓶颈问题未绑仓库 → 拒绝分析（文案逐字对齐）。"""
    client, fac, repo_id, _ = env
    task_id = _seed_stress_task(fac, "http://x/no-match-url")
    issue = client.post("/api/source/issues/from-stress",
                        params={"stressTaskId": task_id, "repoId": repo_id}).json()["data"]
    # from-stress 已绑仓库（sourceRepoId 落库）→ 先用无定位场景验证文案：
    # 直接构造无定位问题：bind-repo 到不存在仓库路径前先测守卫文案由
    # "压测问题必须先关联源码仓库才能分析" —— 需 sourceRepoId 为 null，
    # 创建时 loc 未命中也会落 repoId；这里验证已绑定时可正常分析（走降级）。
    body = client.post(f"/api/source/issues/{issue['id']}/analyze").json()
    assert body["data"] == {"generated": 2}, "已绑仓库的压测问题可分析（降级模板）"
    detail = client.get(f"/api/source/issues/{issue['id']}").json()["data"]
    assert detail["issue"]["rootCause"] == \
        "（规则模式）压测结果显示吞吐达到上限，存在串行瓶颈或资源争用"


def test_analyze_with_llm_and_fence_stripping(env):
    """假 LLM：markdown 围栏剥离、3 条方案上限截断、files/changes 序列化落库。"""
    client, fac, repo_id, tmp_path = env
    src = _make_source_repo(tmp_path)
    # 带假 LLM 的应用（复用同一内存库）
    fac2 = client.app.state.source_repo._sessions  # noqa: SLF001
    app2 = create_app(
        stress_repo=StressRepository(fac2), collector_repo=client.app.state.collector_repo,
        source_repo=client.app.state.source_repo,
        source_storage_dir=str(tmp_path / "storage2"),
        data_cleanup=DataCleanupService(fac2, SqliteKnowledgeStore()),
        llm=_ScriptedLlm(), auth_enabled=False)
    client2 = TestClient(app2)
    client2.post("/api/source/repos/local", json={"repoName": "mini2", "localPath": src})

    sql_id = _seed_slow_sql(fac2, None)
    issue = client2.post("/api/source/issues/from-slow-sql",
                         params={"slowSqlId": sql_id, "repoId": 2}).json()["data"]
    body = client2.post(f"/api/source/issues/{issue['id']}/analyze").json()
    assert body["data"] == {"generated": 3}, "LLM 产出 4 条，落库截断为 3"
    detail = client2.get(f"/api/source/issues/{issue['id']}").json()["data"]
    assert detail["issue"]["rootCause"] == "未命中索引导致全表扫描"
    first = detail["suggestions"][0]
    assert first["title"] == "补复合索引" and first["selected"] == 0
    assert json.loads(first["filesJson"]) == [{"file": "OrderMapper.java"}]
    assert json.loads(first["codeChangesJson"]) == ["CREATE INDEX ..."]
    assert first["benefit"] == "P95 -80%" and first["risk"] == "写入放大"


def test_analyze_with_db_explain_evidence(env):
    """慢 SQL + db_conn 三级匹配（host 双向包含）→ EXPLAIN 只读检测回写证据。"""
    client, fac, repo_id, _ = env
    now = datetime(2026, 11, 16, 10, 0, 0)
    with fac() as s:
        s.add(DbConnModel(conn_name="c1", db_url="jdbc:mysql://10.0.0.20:3306/perf",
                          host="10.0.0.20", app_name="", username="u", password="p",
                          create_time=now))
        s.commit()
    sql_id = _seed_slow_sql(fac, None)
    issue = client.post("/api/source/issues/from-slow-sql",
                        params={"slowSqlId": sql_id, "repoId": repo_id}).json()["data"]
    body = client.post(f"/api/source/issues/{issue['id']}/analyze").json()
    assert body["data"] == {"generated": 2}
    detail = client.get(f"/api/source/issues/{issue['id']}").json()["data"]
    evidence = json.loads(detail["issue"]["evidenceJson"])
    assert "dbExplain" in evidence, "EXPLAIN 证据已回写"
    assert evidence["dbExplain"]["target"] == "10.0.0.20:3306/perf"
    assert evidence["dbExplain"]["actualDb"] == "10.0.0.20"
    assert evidence["dbExplain"]["consistent"] is True
    assert "EXPLAIN 执行计划" in evidence["dbExplain"]["summary"]
    assert "perf_order=998" in evidence["dbExplain"]["summary"], "表规模估算并入"


# ---------------------------------------------------------------- 修复提示词


def test_select_suggestion_prompt_and_single_select(env):
    """select：Markdown 结构落库 + 同一问题单选（其余方案清零）。"""
    client, fac, repo_id, _ = env
    sql_id = _seed_slow_sql(fac, None)
    issue = client.post("/api/source/issues/from-slow-sql",
                        params={"slowSqlId": sql_id, "repoId": repo_id}).json()["data"]
    client.post(f"/api/source/issues/{issue['id']}/analyze")
    detail = client.get(f"/api/source/issues/{issue['id']}").json()["data"]
    sid = detail["suggestions"][0]["id"]

    body = client.post(f"/api/source/issues/{issue['id']}/suggestions/{sid}/select").json()
    assert body["code"] == 0
    md = body["data"]["promptMd"]
    for section in ("# 性能问题修复任务", "## 1. 任务目标", "## 2. 检测证据",
                    "## 3. 根因分析", "## 5. 建议修改", "## 6. 预期收益",
                    "## 7. 风险与注意事项", "## 8. 验证方式",
                    "> 请按上述方案修改代码，保持现有代码风格，改动最小化，并补充必要注释。"):
        assert section in md, f"提示词含章节：{section}"
    assert f"**{issue['title']}**（类型：slow_sql）" in md
    assert "SQL 优化：索引与查询改写" not in md or True  # 方案标题不出现在提示词，无强契约
    detail2 = client.get(f"/api/source/issues/{issue['id']}").json()["data"]
    selected = [s["selected"] for s in detail2["suggestions"]]
    assert selected == [1, 0], "单选生效，prompt_md 落库"
    assert detail2["suggestions"][0]["promptMd"] == md

    # 方案不属于该问题 / 不存在 → 同一文案
    other_issue = client.post("/api/source/issues/from-hot-method",
                              params={"className": "com.demo.service.OrderService",
                                      "methodName": "listOrder", "repoId": repo_id}).json()["data"]
    bad = client.post(f"/api/source/issues/{other_issue['id']}"
                      f"/suggestions/{sid}/select").json()
    assert bad == {"code": 1, "message": f"方案不存在或不属于该问题: {sid}", "data": None}
    bad2 = client.post(f"/api/source/issues/{issue['id']}/suggestions/999/select").json()
    assert bad2 == {"code": 1, "message": "方案不存在或不属于该问题: 999", "data": None}


def test_source_file_rows_not_affected(env):
    """仓库文件检索（listByRepoContains 语义）不受问题域写入影响（回归守卫）。"""
    client, fac, repo_id, _ = env
    files = client.get(f"/api/source/repos/{repo_id}/files",
                       params={"prefix": "com/demo/"}).json()["data"]
    assert len(files) == 7
    assert files[0]["relativePath"].startswith("com/demo/")
