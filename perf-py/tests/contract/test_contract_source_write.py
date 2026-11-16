"""source 域写路径契约测试（阶段 5 · 步骤 9）：以 Java SourceController/SourceManager/
SourceFileService/DataCleanupService 源码为契约。

覆盖：zip/git/local 三通道导入（导入即绑定应用、异步 2 线程池语义）、resync 快照语义、
分支枚举/切换（file:// 远端实 clone）、绑定应用与项目准入缓存失效、cleanup-data 级联清理
（全局结论二次确认）、文件内容懒加载、错误文案逐字对齐。
"""

from __future__ import annotations

import io
import subprocess
import time
import zipfile
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from server.app import create_app
from server.collector_repo import (
    AppInstanceModel,
    CollectorRepository,
    DbConnModel,
    JvmMetricModel,
    SlowCommandModel,
    SlowSqlModel,
    SourceRepoModel,
    TraceChainModel,
)
from server.db import make_engine
from server.knowledge import KnowledgeItem, SqliteKnowledgeStore
from server.source_index import SourceFileIndexer
from server.source_repo import (
    FixSuggestionModel,
    PerformanceIssueModel,
    SourceRepository,
)
from server.source_write_service import DataCleanupService, SourceWriteService
from server.stress_repo import (
    Base,
    StressRepository,
    StressSampleModel,
    StressTaskModel,
)


def _git(*args: str, cwd: str | None = None) -> None:
    cmd = ["git", *args] if cwd is None else ["git", "-C", cwd, *args]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _make_origin(tmp_path) -> str:
    """构造 file:// git 远端：main 分支 a.txt，dev 分支多一个 b.txt。

    commit 一律 -c commit.gpgsign=false：CI 环境可能强制 gpg 签名（CNB hook），
    测试远端仓库无需签名。
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("-c", "init.defaultBranch=main", "init", cwd=str(origin))
    (origin / "a.txt").write_text("main content", encoding="utf-8")
    _git("add", ".", cwd=str(origin))
    _git("-c", "commit.gpgsign=false", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", "init", cwd=str(origin))
    _git("checkout", "-b", "dev", cwd=str(origin))
    (origin / "b.txt").write_text("dev content", encoding="utf-8")
    _git("add", ".", cwd=str(origin))
    _git("-c", "commit.gpgsign=false", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", "dev", cwd=str(origin))
    _git("checkout", "main", cwd=str(origin))
    return f"file://{origin}"


def _make_local_src(tmp_path, name="src") -> str:
    """本地源码目录：2 个可索引文件 + 2 个应被忽略项。"""
    d = tmp_path / name
    d.mkdir()
    (d / "Main.java").write_text("class Main {}", encoding="utf-8")
    (d / "pom.xml").write_text("<project/>", encoding="utf-8")
    (d / "App.class").write_bytes(b"\xca\xfe\xba\xbe")    # 忽略：*.class
    sub = d / "build" / "target"
    sub.mkdir(parents=True)
    (sub / "Gen.java").write_text("class Gen {}", encoding="utf-8")  # 忽略：/target/（嵌套）
    return str(d)


class _FailingIndexer:
    """失败注入：导入线程池异常路径的触发器。"""

    def index_directory(self, repo_id, root_dir):
        raise ValueError("索引失败（注入）")


@pytest.fixture()
def env(tmp_path):
    """内存库 + 注入式写服务（inline 同步导入）+ 清理服务（含独立知识库存储）。"""
    fac = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    Base.metadata.create_all(fac.kw["bind"])
    collector = CollectorRepository(fac)
    knowledge = SqliteKnowledgeStore()
    app = create_app(
        stress_repo=StressRepository(fac), collector_repo=collector,
        source_repo=SourceRepository(fac),
        source_storage_dir=str(tmp_path / "storage"),
        data_cleanup=DataCleanupService(fac, knowledge),
        auth_enabled=False)
    return TestClient(app), collector, fac, tmp_path, knowledge


# ---------------------------------------------------------------- 导入 wire 契约


def test_import_local_wire_contract_and_indexing(env):
    """local 导入：响应为刚落库实体形态（gitToken 永不返回、fileCount/createTime null）；
    导入完成后 status=ready，索引含语言识别与忽略规则语义。"""
    client, _, _, tmp_path, _ = env
    src = _make_local_src(tmp_path)
    body = client.post("/api/source/repos/local",
                       json={"repoName": "demo", "localPath": src}).json()
    assert body["code"] == 0
    data = body["data"]
    assert set(data) == {"id", "repoName", "repoType", "sourcePath", "branch", "appName",
                         "gitUser", "status", "fileCount", "totalSize", "errorMsg",
                         "createTime"}
    assert "gitToken" not in data, "git 凭据永不返回"
    assert data["status"] == "importing" and data["fileCount"] is None
    assert data["createTime"] is None and data["appName"] == "demo"  # 导入即绑定

    repos = client.get("/api/source/repos").json()["data"]
    assert repos[0]["status"] == "ready" and repos[0]["fileCount"] == 2
    files = client.get(f"/api/source/repos/{data['id']}/files").json()["data"]
    assert [f["relativePath"] for f in files] == ["Main.java", "pom.xml"]
    assert files[0]["language"] == "java" and files[1]["language"] == "xml"
    assert all(len(f["contentHash"]) == 64 for f in files), "SHA-256 content_hash"


def test_import_local_name_derivation_and_default_app(env):
    """repoName 未填 → local 取路径末段；appName 缺省 = repoName（导入即绑定）。"""
    client, _, _, tmp_path, _ = env
    src = _make_local_src(tmp_path, name="my-app")
    data = client.post("/api/source/repos/local",
                       json={"localPath": src}).json()["data"]
    assert data["repoName"] == "my-app" and data["appName"] == "my-app"


def test_import_zip_multipart_and_index(env):
    """zip 上传导入：落盘 → 解压 → 索引；file_count/total_size 元数据回填。"""
    client, _, _, tmp_path, _ = env
    src = _make_local_src(tmp_path, name="zipsrc")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in ("Main.java", "pom.xml"):
            zf.write(f"{src}/{name}", arcname=name)
    body = client.post("/api/source/repos/zip",
                       data={"repoName": "zip-demo", "appName": "zapp"},
                       files={"file": ("demo.zip", buf.getvalue(),
                                       "application/zip")}).json()
    assert body["code"] == 0 and body["data"]["status"] == "importing"
    repo = client.get("/api/source/repos").json()["data"][0]
    assert repo["repoName"] == "zip-demo" and repo["repoType"] == "zip"
    assert repo["status"] == "ready" and repo["fileCount"] == 2 and repo["totalSize"] > 0


def test_import_zip_empty_file_message(env):
    client, _, _, _, _ = env
    body = client.post("/api/source/repos/zip", data={"repoName": "x"},
                       files={"file": ("empty.zip", b"", "application/zip")}).json()
    assert body == {"code": 1, "message": "请选择要上传的 zip 文件", "data": None}


def test_import_zip_path_traversal_rejected(env):
    """zip 路径穿越检查：非法路径文案逐字对齐，导入中止。"""
    client, _, _, _, _ = env
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", "boom")
    body = client.post("/api/source/repos/zip", data={"repoName": "evil"},
                       files={"file": ("e.zip", buf.getvalue(), "application/zip")}).json()
    assert body == {"code": 1, "message": "zip 包含非法路径，已中止解压: ../evil.txt",
                    "data": None}


def test_import_zip_single_file_over_limit(env):
    client, _, _, _, _ = env
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("big.txt", b"x" * (1024 * 1024 + 1))
    body = client.post("/api/source/repos/zip", data={"repoName": "big"},
                       files={"file": ("b.zip", buf.getvalue(), "application/zip")}).json()
    assert body == {"code": 1, "message": "单文件超过 1MB 上限: big.txt", "data": None}


# ---------------------------------------------------------------- 异步导入线程池语义


def test_async_import_thread_pool_and_failure_status(tmp_path):
    """inline=False：导入进线程池异步执行；异常路径 status='failed' + error_msg；
    正常路径终态 ready（对齐 Java executor 提交体 catch → updateStatus）。"""
    fac = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    Base.metadata.create_all(fac.kw["bind"])
    repo = SourceRepository(fac)
    svc = SourceWriteService(repo, _FailingIndexer(), storage_dir=str(tmp_path / "s"),
                             inline_import=False)
    src = tmp_path / "src"
    src.mkdir()
    (src / "A.java").write_text("class A {}", encoding="utf-8")
    data = svc.start_import("bad", "local", str(src))
    for _ in range(100):
        row = repo.repo_by_id(data["id"])
        if row.status != "importing":
            break
        time.sleep(0.05)
    assert row.status == "failed" and row.error_msg == "索引失败（注入）"

    svc2 = SourceWriteService(repo, SourceFileIndexer(repo),
                              storage_dir=str(tmp_path / "s"), inline_import=False)
    data2 = svc2.start_import("ok", "local", str(src))
    for _ in range(100):
        row2 = repo.repo_by_id(data2["id"])
        if row2.status != "importing":
            break
        time.sleep(0.05)
    assert row2.status == "ready" and row2.file_count == 1 and row2.total_size > 0


# ---------------------------------------------------------------- 校验文案逐字对齐


def test_validation_messages_verbatim(env):
    client, _, _, _, _ = env
    assert client.post("/api/source/repos/git", json={}).json() == {
        "code": 1, "message": "git 地址不能为空", "data": None}
    assert client.post("/api/source/repos/local", json={}).json() == {
        "code": 1, "message": "本地路径不能为空", "data": None}
    body = client.post("/api/source/repos/git",
                       json={"gitUrl": "ssh://git@example.com/a.git"}).json()
    assert body == {"code": 1, "message": "git 地址仅支持 http/https/file 协议", "data": None}
    body = client.post("/api/source/repos/local",
                       json={"repoName": "n", "localPath": "/no/such/dir-xyz"}).json()
    assert body["code"] == 1
    assert body["message"].startswith("本地目录不存在: /no/such/dir-xyz")


def test_repo_not_found_message(env):
    client, _, _, _, _ = env
    assert client.post("/api/source/repos/999/resync").json() == {
        "code": 1, "message": "仓库不存在: 999", "data": None}
    assert client.get("/api/source/repos/999/branches").json() == {
        "code": 1, "message": "仓库不存在: 999", "data": None}
    assert client.post("/api/source/repos/999/bind-app", json={"appName": "a"}).json() == {
        "code": 1, "message": "仓库不存在: 999", "data": None}


# ---------------------------------------------------------------- git 全链（file:// 远端）


def test_git_import_branches_switch_and_resync(env):
    """git 导入 → 分支枚举（ls-remote 直连）→ 切换分支（远端校验 + resync 重建索引）。"""
    client, _, _, tmp_path, _ = env
    url = _make_origin(tmp_path)
    data = client.post("/api/source/repos/git",
                       json={"repoName": "git-demo", "gitUrl": url,
                             "branch": "main"}).json()["data"]
    assert client.get("/api/source/repos").json()["data"][0]["status"] == "ready"
    files = client.get(f"/api/source/repos/{data['id']}/files").json()["data"]
    assert [f["fileName"] for f in files] == ["a.txt"], "main 分支仅 a.txt（.git 忽略）"

    # 分支列表：直连远端枚举，字母序
    body = client.get(f"/api/source/repos/{data['id']}/branches").json()
    assert body["data"] == ["dev", "main"]
    body = client.post("/api/source/repos/git-branches", json={"gitUrl": url}).json()
    assert body["data"] == ["dev", "main"]

    # 切换分支：远端校验通过 → 更新配置 → resync 重建索引
    body = client.post(f"/api/source/repos/{data['id']}/branch",
                       json={"branch": "dev"}).json()
    assert body["data"] == {"repoId": data["id"], "branch": "dev", "indexed": 2}
    files = client.get(f"/api/source/repos/{data['id']}/files").json()["data"]
    assert sorted(f["fileName"] for f in files) == ["a.txt", "b.txt"]
    repo = client.get("/api/source/repos").json()["data"][0]
    assert repo["branch"] == "dev"

    # 非法分支：远端不存在 → 文案含可用分支列表（Java List.toString 形态）
    body = client.post(f"/api/source/repos/{data['id']}/branch",
                       json={"branch": "nope"}).json()
    assert body["code"] == 1
    assert body["message"] == "远端不存在分支: nope，可用分支: ['dev', 'main']"
    assert client.post(f"/api/source/repos/{data['id']}/branch", json={}).json() == {
        "code": 1, "message": "目标分支不能为空", "data": None}


def test_git_only_guards(env):
    """非 git 仓库：分支列表/切换分支专属文案逐字对齐。"""
    client, _, _, tmp_path, _ = env
    src = _make_local_src(tmp_path)
    data = client.post("/api/source/repos/local",
                       json={"repoName": "loc", "localPath": src}).json()["data"]
    assert client.get(f"/api/source/repos/{data['id']}/branches").json() == {
        "code": 1, "message": "仅 git 仓库支持分支列表", "data": None}
    assert client.post(f"/api/source/repos/{data['id']}/branch",
                       json={"branch": "main"}).json() == {
        "code": 1, "message": "仅 git 仓库支持切换分支", "data": None}


# ---------------------------------------------------------------- resync 快照语义


def test_resync_rebuilds_snapshot(env):
    """local resync：先删光副本重新复制（快照语义），索引全量重建（增/删/改都同步）。"""
    client, _, _, tmp_path, _ = env
    src = tmp_path / "snap"
    src.mkdir()
    (src / "a.java").write_text("class A {}", encoding="utf-8")
    (src / "b.java").write_text("class B {}", encoding="utf-8")
    data = client.post("/api/source/repos/local",
                       json={"repoName": "snap", "localPath": str(src)}).json()["data"]
    repo_id = data["id"]

    # 源目录变更：删 b、增 c
    (src / "b.java").unlink()
    (src / "c.java").write_text("class C {}", encoding="utf-8")
    body = client.post(f"/api/source/repos/{repo_id}/resync").json()
    assert body["data"] == {"repoId": repo_id, "indexed": 2}
    files = client.get(f"/api/source/repos/{repo_id}/files").json()["data"]
    assert sorted(f["fileName"] for f in files) == ["a.java", "c.java"], "全量重建"


def test_resync_guards(env):
    """resync 守卫：importing 拒绝并发；缺来源信息文案逐字对齐。"""
    client, _, fac, _, _ = env
    now = datetime(2026, 1, 1, 9, 0, 0)
    with fac() as s:
        s.add(SourceRepoModel(repo_name="busy", repo_type="local", source_path="",
                              app_name="busy", status="importing", create_time=now))
        s.commit()
        busy_id = s.scalars(select(SourceRepoModel.id)).first()
    assert client.post(f"/api/source/repos/{busy_id}/resync").json() == {
        "code": 1, "message": "服务器内部错误: 仓库正在导入中，请稍后再试", "data": None}
    # （Java IllegalStateException → GlobalExceptionHandler.handleException → 前缀包装）
    with fac() as s:
        row = s.get(SourceRepoModel, busy_id)
        row.status = "ready"
        s.commit()
    assert client.post(f"/api/source/repos/{busy_id}/resync").json() == {
        "code": 1, "message": "仓库缺少来源信息，无法重新同步", "data": None}


# ---------------------------------------------------------------- 绑定与缓存失效


def test_bind_app_and_project_cache_evict(env):
    """绑定应用：空串=解绑；导入后 evict 项目准入缓存（30s 窗口立即可识别新项目）。"""
    client, collector, _, tmp_path, _ = env
    src = _make_local_src(tmp_path, name="bind-src")
    assert collector.project_exists("bind") is False   # 预热：False 入缓存
    data = client.post("/api/source/repos/local",
                       json={"repoName": "bind", "localPath": src}).json()["data"]
    assert collector.project_exists("bind") is True, "导入后缓存失效并重查命中"

    body = client.post(f"/api/source/repos/{data['id']}/bind-app",
                       json={"appName": "app-x"}).json()
    assert body["data"] == {"repoId": data["id"], "appName": "app-x"}
    repo = client.get("/api/source/repos").json()["data"][0]
    assert repo["appName"] == "app-x"
    body = client.post(f"/api/source/repos/{data['id']}/bind-app",
                       json={"appName": ""}).json()
    assert body["data"]["appName"] == "", "空串 = 解绑"


# ---------------------------------------------------------------- cleanup-data 级联清理


def _seed_cleanup_data(fac, app_name: str) -> None:
    now = datetime(2026, 11, 16, 10, 0, 0)
    with fac() as s:
        task = StressTaskModel(task_name="t", target_url="http://x", app_name=app_name,
                               create_time=now)
        s.add(task)
        s.flush()
        s.add(StressSampleModel(task_id=task.id, sample_time=now))
        issue = PerformanceIssueModel(issue_type="slow_sql", app_name=app_name,
                                      title="t", create_time=now)
        s.add(issue)
        s.flush()
        s.add(FixSuggestionModel(issue_id=issue.id, title="f", create_time=now))
        s.add(TraceChainModel(trace_id=f"tr-{app_name}", app_name=app_name, start_time=now))
        s.add(JvmMetricModel(app_name=app_name, collect_time=now))
        s.add(SlowSqlModel(app_name=app_name, sql_text="SELECT 1", execute_time=now))
        s.add(SlowCommandModel(app_name=app_name, type="redis", execute_time=now))
        s.add(AppInstanceModel(app_name=app_name, create_time=now))
        s.add(DbConnModel(conn_name="c", db_url="jdbc:mysql://h/db", app_name=app_name,
                          create_time=now))
        s.commit()


def test_cleanup_data_cascade_and_global_guard(env):
    """cleanup-data：按 appName 级联清理 10 张表；全局 AI 结论默认保留，
    开启需 confirmAppName 精确回显（文案逐字对齐）。"""
    client, _, fac, tmp_path, knowledge = env
    src = _make_local_src(tmp_path, name="dirty-src")
    data = client.post("/api/source/repos/local",
                       json={"repoName": "dirty", "localPath": src}).json()["data"]
    _seed_cleanup_data(fac, "dirty")
    knowledge.save(KnowledgeItem(app_name="dirty", title="t", content="c" * 50,
                                 create_time="2026-11-16T10:00:00"))
    knowledge.save(KnowledgeItem(app_name="", title="全局", content="g" * 50,
                                 create_time="2026-11-16T10:00:01"))

    # 未开全局清理：本应用结论删除，全局保留
    body = client.post(f"/api/source/repos/{data['id']}/cleanup-data", json={}).json()
    assert body["code"] == 0
    assert body["data"]["appName"] == "dirty"
    assert body["data"]["includeGlobalKnowledge"] is False
    deleted = body["data"]["deleted"]
    assert set(deleted) == {"stressSample", "stressTask", "fixSuggestion",
                            "performanceIssue", "traceChain", "jvmMetric", "slowSql",
                            "slowCommand", "appInstance", "dbConn", "aiKnowledge",
                            "aiKnowledgeGlobal"}
    assert deleted["stressSample"] == 1 and deleted["stressTask"] == 1
    assert deleted["fixSuggestion"] == 1 and deleted["performanceIssue"] == 1
    assert deleted["traceChain"] == 1 and deleted["jvmMetric"] == 1
    assert deleted["slowSql"] == 1 and deleted["slowCommand"] == 1
    assert deleted["appInstance"] == 1 and deleted["dbConn"] == 1
    assert deleted["aiKnowledge"] == 1 and deleted["aiKnowledgeGlobal"] == 0
    assert len(knowledge.recent("", 10)) == 1, "全局结论保留"

    # 开全局清理但未回显 confirmAppName → 拒绝
    body = client.post(f"/api/source/repos/{data['id']}/cleanup-data",
                       json={"includeGlobalKnowledge": True}).json()
    assert body == {"code": 1, "message": "开启全局知识清理需二次确认：请求体 confirmAppName "
                                        "必须与项目绑定的 appName 完全一致", "data": None}
    # 精确回显 → 全局结论删除
    body = client.post(f"/api/source/repos/{data['id']}/cleanup-data",
                       json={"includeGlobalKnowledge": True,
                             "confirmAppName": "dirty"}).json()
    assert body["data"]["deleted"]["aiKnowledgeGlobal"] == 1
    assert knowledge.recent("", 10) == []


def test_cleanup_data_unbound_repo(env):
    """未绑定 appName：清理拒绝文案逐字对齐。"""
    client, _, fac, _, _ = env
    now = datetime(2026, 1, 1, 9, 0, 0)
    with fac() as s:
        s.add(SourceRepoModel(repo_name="noapp", repo_type="local", source_path="/x",
                              app_name="", status="ready", create_time=now))
        s.commit()
        rid = s.scalars(select(SourceRepoModel.id)).first()
    assert client.post(f"/api/source/repos/{rid}/cleanup-data", json={}).json() == {
        "code": 1, "message": "项目未绑定应用（appName），无法定位历史数据，"
                              "请先在项目列表绑定应用", "data": None}


# ---------------------------------------------------------------- 文件内容懒加载


def test_file_content_endpoint(env):
    """GET /files/{fileId}/content：懒加载磁盘内容；缺失 → code=1「文件不存在」。"""
    client, _, _, tmp_path, _ = env
    src = _make_local_src(tmp_path, name="content-src")
    data = client.post("/api/source/repos/local",
                       json={"repoName": "content", "localPath": src}).json()["data"]
    files = client.get(f"/api/source/repos/{data['id']}/files").json()["data"]
    target = next(f for f in files if f["fileName"] == "Main.java")
    body = client.get(f"/api/source/files/{target['id']}/content").json()
    assert body["code"] == 0
    assert set(body["data"]) == {"relativePath", "language", "content"}
    assert body["data"]["content"] == "class Main {}"
    assert body["data"]["language"] == "java"
    assert client.get("/api/source/files/999/content").json() == {
        "code": 1, "message": "文件不存在: 999", "data": None}
