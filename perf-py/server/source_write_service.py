"""源码仓库导入/同步/分支/绑定与项目数据清理（阶段 5 · 步骤 9）：
Java SourceManager + DataCleanupService 的逐语义移植。

对齐点（Java → Python 异常映射）：
- IllegalArgumentException → ValueError（参数/校验错误）
- IllegalStateException   → RuntimeError（git 执行/状态错误）
- IOException（受检异常，resync 中被包装）→ OSError → RuntimeError("重新同步失败: X")

关键语义：
- start_import：校验 → 名称派生 → status='importing' 落库 → evict 项目缓存
  → 固定 2 线程池异步导入（线程名 source-import；异常 → status='failed' + error_msg）。
  导入响应为刚落库的实体形态：status=importing、fileCount/totalSize/createTime 为 null
  （Java 实体 insert 后未回填），gitToken @JsonIgnore 永不返回。
- resync：同步执行。importing 中拒绝（"仓库正在导入中"）；git 副本保留 .git 做
  fetch+checkout -B+reset --hard+clean -fd（无 .git 则重新 clone）；zip/local 先删光副本
  （快照语义）再重新拉取；全量重建索引（先 deleteByRepo）；失败 → status='failed'。
- zip 解压三重安全：拒绝 ".."/绝对路径/越界 resolve；单文件 1MB / 总量 200MB / 文件数 5000。
- git：仅 http/https/file 协议；clone --depth 1 --no-single-branch（保留全分支远端引用）；
  凭据注入 URL userinfo（仅用于命令参数，禁止落库/日志）；HEAD 悬空回退 origin/main/master。
- cleanup-data：按绑定 appName 级联清理压测/慢SQL/慢命令/JVM/调用链/问题/方案/实例/
  db_conn/AI 结论；全局结论（app_name=''）默认保留，显式开启需 confirmAppName 二次确认。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote_plus

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from server.collector_repo import (
    AppInstanceModel,
    DbConnModel,
    JvmMetricModel,
    SlowCommandModel,
    SlowSqlModel,
    SourceRepoModel,
    TraceChainModel,
)
from server.knowledge import KnowledgeStore
from server.source_index import SourceFileIndexer
from server.source_repo import (
    FixSuggestionModel,
    PerformanceIssueModel,
    SourceRepository,
)
from server.stress_repo import StressSampleModel, StressTaskModel

log = logging.getLogger(__name__)

MAX_SINGLE_FILE = 1024 * 1024          # 1MB（zip 解压单文件上限）
MAX_TOTAL_SIZE = 200 * 1024 * 1024     # 200MB（zip 解压总量上限）
MAX_FILE_COUNT = 5000                  # zip 解压文件数上限
COPY_DEPTH_LIMIT = 20                  # 本地目录复制深度上限（对齐 Files.walk(source, 20)）
BRANCH_TIMEOUT_S = 30                  # ls-remote 超时（防挂起）
IMPORT_WORKERS = 2                     # 导入线程池大小（对齐 Java newFixedThreadPool(2)）
DEFAULT_STORAGE_DIR = "/data/perf-source"


class SourceWriteService:
    """SourceManager 移植：zip 上传/git clone/本地目录导入 + resync + 分支 + 绑定应用。"""

    def __init__(self, repo: SourceRepository, indexer: SourceFileIndexer,
                 storage_dir: str | None = None, collector_repo=None,
                 inline_import: bool = False):
        self._repo = repo
        self._indexer = indexer
        self._collector = collector_repo
        self.storage_dir = storage_dir if storage_dir is not None else (
            os.environ.get("PERF_SOURCE_STORAGE_DIR") or DEFAULT_STORAGE_DIR)
        # inline_import：进程内同步执行导入（:memory: 测试库；否则异步进 2 线程池）
        self._executor = None if inline_import else ThreadPoolExecutor(
            max_workers=IMPORT_WORKERS, thread_name_prefix="source-import")

    # ------------------------------------------------------------ 导入

    def start_import(self, repo_name: str | None, repo_type: str, payload: str | None,
                     branch: str | None = None, git_user: str | None = None,
                     git_token: str | None = None, app_name: str | None = None) -> dict:
        """创建仓库记录并异步导入；返回刚落库的实体 wire 形态（对齐 Java）。"""
        type_ = (repo_type or "zip").lower()
        if type_ not in ("zip", "git", "local"):
            raise ValueError(f"不支持的导入类型: {type_}")
        if not payload or not payload.strip():
            raise ValueError("导入来源不能为空（zip 路径 / git 地址 / 本地目录）")
        # 名称未填时自动派生：git 取 URL 末段、local 取路径末段（zip 由上传侧按文件名传入）
        name = derive_repo_name(type_, payload) if not repo_name or not repo_name.strip() \
            else repo_name.strip()
        if not name.strip():
            raise ValueError("仓库名称不能为空")
        repo = SourceRepoModel(
            repo_name=name,
            repo_type=type_,
            source_path=payload,
            branch=(branch or "").strip(),
            # 应用名 = 项目（仓库）名，agent 上报 appName 应与项目名一致
            app_name=(app_name.strip() if app_name and app_name.strip() else name),
            git_user=(git_user or "").strip(),
            git_token=(git_token or "").strip(),
            status="importing",
            create_time=datetime.now(),
        )
        repo_id = self._repo.insert_repo(repo)
        # 新建仓库立即可被 agent 上报认作"已知项目"（否则 30s 缓存窗口内的上报会被整批丢弃）
        self._evict(name)
        job = lambda: self._run_import(repo_id, type_, payload)  # noqa: E731
        if self._executor is None:
            job()
        else:
            self._executor.submit(self._guarded_import, repo_id, job)
        # Java 返回 insert 后的实体：fileCount/totalSize/createTime 未回填（null）
        return {
            "id": repo_id,
            "repoName": name,
            "repoType": type_,
            "sourcePath": payload,
            "branch": (branch or "").strip(),
            "appName": (app_name.strip() if app_name and app_name.strip() else name),
            "gitUser": (git_user or "").strip(),
            "status": "importing",
            "fileCount": None,
            "totalSize": None,
            "errorMsg": None,
            "createTime": None,
        }

    def _guarded_import(self, repo_id: int, job) -> None:
        """异步导入包装：异常 → status='failed'（对齐 Java executor 提交体的 catch）。"""
        try:
            job()
        except Exception as exc:  # noqa: BLE001
            log.error("source import failed, repoId=%s", repo_id, exc_info=exc)
            self._repo.update_status(repo_id, "failed", str(exc) or repr(exc))

    def _run_import(self, repo_id: int, type_: str, payload: str) -> None:
        """doImport：拉取源码到磁盘根目录 → 建索引 → updateIndexed（status='ready'）。"""
        root_dir = self.repo_root_dir(repo_id)
        os.makedirs(root_dir, exist_ok=True)
        repo = self._repo.repo_by_id(repo_id)
        if type_ == "zip":
            self.unzip_safe(payload, root_dir)
        elif type_ == "git":
            self.git_clone(payload, root_dir, repo.branch, repo.git_user, repo.git_token)
        elif type_ == "local":
            self.copy_local_dir(payload, root_dir)
        else:
            raise ValueError(f"不支持的导入类型: {type_}")
        indexed = self._indexer.index_directory(repo_id, root_dir)
        total_size = size_of(root_dir)
        self._repo.update_indexed(repo_id, indexed, total_size)
        log.info("source repo %s imported, indexed=%s", repo_id, indexed)

    def save_zip(self, data: bytes, original_name: str | None) -> str:
        """保存上传的 zip 到存储目录，返回磁盘路径（对齐 saveZip）。"""
        upload_dir = os.path.join(self.storage_dir, "uploads")
        os.makedirs(upload_dir, exist_ok=True)
        safe_name = "upload.zip" if not original_name \
            else os.path.basename(original_name.replace("\\", "/"))
        target = os.path.join(upload_dir, f"{uuid.uuid4()}-{safe_name}")
        with open(target, "wb") as out:
            out.write(data)
        return target

    # ------------------------------------------------------------ resync

    def resync(self, repo_id: int) -> int:
        """重新同步仓库（同步执行）：清空平台副本 → 按原类型重新拉取 → 重建文件索引。"""
        repo = self.get_repo(repo_id)
        if repo.status == "importing":
            raise RuntimeError("仓库正在导入中，请稍后再试")
        type_ = (repo.repo_type or "local").lower()
        payload = repo.source_path
        if not payload or not payload.strip():
            raise ValueError("仓库缺少来源信息，无法重新同步")
        try:
            root_dir = self.repo_root_dir(repo_id)
            if type_ == "git":
                # git 来源：副本保留 .git，fetch 指定分支 → checkout/重置到远端最新（支持分支切换）
                auth_url = build_auth_url(payload, repo.git_user, repo.git_token)
                os.makedirs(root_dir, exist_ok=True)
                if not os.path.exists(os.path.join(root_dir, ".git")):
                    self.git_clone(payload, root_dir, repo.branch, repo.git_user, repo.git_token)
                else:
                    branch = (repo.branch or "").strip()
                    # 1. fetch 全部分支引用（显式 refspec；凭据注入 URL，origin remote 保持干净）
                    run_git(["git", "-C", root_dir, "fetch", "--depth", "1", auth_url,
                             "+refs/heads/*:refs/remotes/origin/*"], "git fetch 失败")
                    if branch:
                        # 2. 切换/重置到远端分支最新（-B 强制，分支与上次不同也可）
                        run_git(["git", "-C", root_dir, "checkout", "-B", branch,
                                 f"origin/{branch}"], "git checkout 失败")
                    # 3. 清理工作区（忽略本地改动，与远端严格一致）
                    run_git(["git", "-C", root_dir, "reset", "--hard"], "git reset 失败")
                    run_git(["git", "-C", root_dir, "clean", "-fd"], "git clean 失败")
            else:
                # zip / local 来源：清空副本后重新拉取（保持快照语义）
                if os.path.exists(root_dir):
                    _remove_tree(root_dir)
                os.makedirs(root_dir, exist_ok=True)
                if type_ == "zip":
                    self.unzip_safe(payload, root_dir)
                else:
                    self.copy_local_dir(payload, root_dir)
            # 全量重建索引（增/删/改都同步）
            self._repo.delete_files_by_repo(repo_id)
            indexed = self._indexer.index_directory(repo_id, root_dir)
            total_size = size_of(root_dir)
            self._repo.update_indexed(repo_id, indexed, total_size)
            self._repo.update_status(repo_id, "ready", None)
            self._evict(repo.repo_name)
            log.info("source repo %s resynced (%s), indexed=%s", repo_id, type_, indexed)
            return indexed
        except (ValueError, RuntimeError):
            raise
        except Exception as exc:  # noqa: BLE001 受检异常包装（对齐 IOException 分支）
            raise RuntimeError(f"重新同步失败: {exc}") from exc

    # ------------------------------------------------------------ 分支

    def list_remote_branches(self, url: str | None, git_user: str | None,
                             git_token: str | None) -> list[str]:
        """枚举远端分支（无需 clone）：git ls-remote --heads 直连，30s 超时防挂起。"""
        if not url or not url.strip() or not url.startswith(("http://", "https://", "file://")):
            raise ValueError("git 地址仅支持 http/https/file 协议")
        try:
            proc = subprocess.run(
                ["git", "ls-remote", "--heads", build_auth_url(url.strip(), git_user, git_token)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=BRANCH_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired:
            raise RuntimeError("获取分支超时，请检查地址与网络") from None
        except FileNotFoundError:
            raise RuntimeError("git 命令执行失败，请确认服务器已安装 git") from None
        if proc.returncode != 0:
            raise RuntimeError("无法访问远端仓库，请检查地址/网络/凭据")
        out = proc.stdout.decode("utf-8", errors="replace")
        branches: list[str] = []
        for line in out.split("\n"):
            idx = line.find("refs/heads/")
            if idx >= 0:
                branches.append(line[idx + len("refs/heads/"):].strip())
        branches.sort()
        return branches

    def list_branches(self, repo_id: int) -> list[str]:
        """仓库分支列表：以远端为准（ls-remote 直连），git 类型专用。"""
        repo = self.get_repo(repo_id)
        if (repo.repo_type or "").lower() != "git":
            raise ValueError("仅 git 仓库支持分支列表")
        return self.list_remote_branches(repo.source_path, repo.git_user, repo.git_token)

    def switch_branch(self, repo_id: int, branch: str | None) -> int:
        """切换分支：校验目标分支存在于远端 → 更新配置 → resync。返回切换后索引文件数。"""
        if not branch or not branch.strip():
            raise ValueError("目标分支不能为空")
        repo = self.get_repo(repo_id)
        if (repo.repo_type or "").lower() != "git":
            raise ValueError("仅 git 仓库支持切换分支")
        target = branch.strip()
        remote = self.list_remote_branches(repo.source_path, repo.git_user, repo.git_token)
        if target not in remote:
            raise ValueError(f"远端不存在分支: {target}，可用分支: {remote}")
        self._repo.update_branch(repo_id, target)
        return self.resync(repo_id)

    # ------------------------------------------------------------ 绑定

    def bind_app(self, repo_id: int, app_name: str | None) -> str:
        """绑定应用：绑定后压测/问题/源码分析自动归属该应用；空串 = 解绑。"""
        repo = self.get_repo(repo_id)
        app = (app_name or "").strip()
        self._repo.update_app_name(repo_id, app)
        # 断掉旧项目名可能残留的缓存，避免改名后仍按旧名放行上报
        self._evict(repo.repo_name)
        return app

    # ------------------------------------------------------------ 磁盘布局

    def get_repo(self, repo_id: int) -> SourceRepoModel:
        repo = self._repo.repo_by_id(repo_id)
        if repo is None:
            raise ValueError(f"仓库不存在: {repo_id}")
        return repo

    def repo_root_dir(self, repo_id: int) -> str:
        """仓库磁盘根目录：<存储目录>/<项目名>-<id>（目录名可读，同名靠 id 区分）。"""
        repo = self._repo.repo_by_id(repo_id)
        name = repo.repo_name if repo is not None and repo.repo_name else "repo"
        return os.path.join(self.storage_dir, f"{sanitize_dir_name(name)}-{repo_id}")

    def _evict(self, repo_name: str | None) -> None:
        if self._collector is not None:
            self._collector.evict_project(repo_name)

    # ------------------------------------------------------------ 拉取实现

    def unzip_safe(self, zip_path: str, dest_dir: str) -> None:
        """zip 解压（防路径穿越：解压路径强制位于目标目录内 + 三重限额）。"""
        if not os.path.exists(zip_path):
            raise ValueError(f"zip 文件不存在: {zip_path}")
        normalized_dest = os.path.abspath(os.path.normpath(dest_dir))
        total = 0
        count = 0
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                if ".." in name or name.startswith("/") or name.startswith("\\"):
                    raise ValueError(f"zip 包含非法路径，已中止解压: {name}")
                target = os.path.abspath(os.path.normpath(os.path.join(normalized_dest, name)))
                if not target.startswith(normalized_dest + os.sep) and target != normalized_dest:
                    raise ValueError(f"zip 路径越界，已中止解压: {name}")
                parent = os.path.dirname(target)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                size = os.path.getsize(target)
                total += size
                count += 1
                if size > MAX_SINGLE_FILE:
                    raise ValueError(f"单文件超过 1MB 上限: {name}")
                if total > MAX_TOTAL_SIZE:
                    raise ValueError("源码总大小超过 200MB 上限")
                if count > MAX_FILE_COUNT:
                    raise ValueError("文件数超过 5000 上限")

    def git_clone(self, url: str, dest_dir: str, branch: str | None,
                  git_user: str | None, git_token: str | None) -> None:
        """git clone（浅克隆深度 1，可指定分支；branch 空 = 远端默认分支）。

        不使用 --single-branch：保留全分支远端引用可供分支列表与切换使用；
        远端 HEAD 悬空（默认分支名不一致，如 master vs main）时回退检出 main 或 master。
        """
        if not url or not url.strip() \
                or not url.startswith(("http://", "https://", "file://")):
            # 放行 file://（与本地目录导入同级风险）；禁止 ssh://、git:// 等需凭据/非预期协议
            raise ValueError("git 地址仅支持 http/https/file 协议")
        cmd = ["git", "clone", "--depth", "1", "--no-single-branch"]
        if branch and branch.strip():
            cmd += ["--branch", branch.strip()]
        cmd += [build_auth_url(url, git_user, git_token), dest_dir]
        run_git(cmd, "git clone 失败")
        if not branch or not branch.strip():
            self._checkout_fallback_if_empty(dest_dir)

    def _checkout_fallback_if_empty(self, dest_dir: str) -> None:
        """clone 后工作区为空（远端 HEAD 悬空）时，依次尝试检出 origin/main、origin/master。"""
        try:
            entries = [e for e in os.listdir(dest_dir) if e != ".git"]
        except OSError:
            return
        if entries:
            return
        for b in ("main", "master"):
            try:
                run_git(["git", "-C", dest_dir, "checkout", "-B", b, f"origin/{b}"],
                        f"checkout {b} 失败")
                return
            except Exception:  # noqa: BLE001 尝试下一个默认分支名
                pass

    def copy_local_dir(self, source: str, dest_dir: str) -> None:
        """本地目录导入：复制到平台存储目录（保证隔离与快照）。"""
        if not os.path.isdir(source):
            raise ValueError(f"本地目录不存在: {source}")
        normalized_dest = os.path.abspath(os.path.normpath(dest_dir))
        for file in _walk_files_limited(source):
            rel = os.path.relpath(file, source).replace("\\", "/")
            if rel.startswith(".git/") or "/target/" in rel or "/node_modules/" in rel:
                continue
            target = os.path.abspath(os.path.normpath(
                os.path.join(normalized_dest, os.path.relpath(file, source))))
            if not target.startswith(normalized_dest + os.sep) and target != normalized_dest:
                raise ValueError(f"本地路径越界: {rel}")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(file, target)


def run_git(cmd: list[str], fail_msg: str) -> None:
    """执行 git 命令：非 0 退出抛业务异常，输出丢弃（对齐 runGit）。"""
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, check=False)
    except FileNotFoundError:
        raise RuntimeError("git 命令执行失败，请确认服务器已安装 git") from None
    if proc.returncode != 0:
        raise RuntimeError(
            f"{fail_msg}（退出码 {proc.returncode}），请检查地址/分支/网络及服务器 git 凭据")


def build_auth_url(url: str | None, git_user: str | None, git_token: str | None) -> str | None:
    """构造带凭据的 git URL：仅 http(s) 注入 userinfo（URL 编码），file:// 原样返回。

    组合规则：user+token → user:token@；仅 token → token@；仅 user → user@。
    注意：返回值含明文凭据，仅用于 git 命令参数，禁止落库与打日志。
    """
    if not url or not url.startswith("http"):
        return url
    user = (git_user or "").strip()
    token = (git_token or "").strip()
    if not user and not token:
        return url
    scheme_end = url.index("://")
    rest = url[scheme_end + 3:]
    userinfo = (quote_plus(user) if user else "") + (f":{quote_plus(token)}" if token else "")
    return f"{url[:scheme_end + 3]}{userinfo}@{rest}"


def sanitize_dir_name(name: str) -> str:
    """目录名净化：仅保留中英文/数字/._-，其余替换下划线（对齐 sanitizeDirName）。"""
    s = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fa5]", "_", name)
    return s if s.strip() else "repo"


def derive_repo_name(type_: str, payload: str) -> str:
    """名称自动派生：git 取 URL 末段（去 .git）、local 取路径末段、zip 去扩展名。"""
    p = re.sub(r"[/\\]+$", "", payload)
    if type_ == "git":
        p = p[p.rfind("/") + 1:]
        if p.endswith(".git"):
            p = p[:-4]
        return p
    if type_ == "local":
        return p[max(0, p.rfind("/") + 1):]
    file_name = os.path.basename(p.replace("\\", "/"))
    return file_name[:-4] if file_name.endswith(".zip") else file_name


def size_of(dir_path: str) -> int:
    """目录总大小（深度 ≤20；单文件读取失败计 0，对齐 sizeOf）。"""
    total = 0
    for file in _walk_files_limited(dir_path):
        try:
            total += os.path.getsize(file)
        except OSError:
            pass
    return total


def _walk_files_limited(root: str) -> list[str]:
    """递归列出常规文件（深度限制 20，对齐 Files.walk(root, 20)）。"""
    result: list[str] = []
    root_depth = os.path.abspath(root).rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(os.path.abspath(root)):
        if dirpath.count(os.sep) - root_depth >= COPY_DEPTH_LIMIT:
            dirnames[:] = []
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.isfile(path):
                result.append(path)
    return result


def _remove_tree(root: str) -> None:
    """删光副本目录（先深后浅，删除失败忽略——对齐 resync 的 zip/local 清空逻辑）。"""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            try:
                os.remove(os.path.join(dirpath, name))
            except OSError:
                pass
        for name in dirnames:
            try:
                os.rmdir(os.path.join(dirpath, name))
            except OSError:
                pass
    try:
        os.rmdir(root)
    except OSError:
        pass


class DataCleanupService:
    """项目历史数据清理（DataCleanupService 移植）：按绑定 appName 级联删除采集与派生数据。

    全局 AI 结论（app_name=''）默认保留；删除在一个 DB 事务内完成
    （AI 结论存 KnowledgeStore，独立连接，事务边界单列——与 Java 单 MySQL 事务的差别已注明）。
    """

    def __init__(self, session_factory: sessionmaker,
                 knowledge_store: KnowledgeStore | None = None):
        self._sessions = session_factory
        self._knowledge = knowledge_store

    def cleanup_by_app_name(self, app_name: str | None,
                            include_global_knowledge: bool = False) -> dict:
        """清理指定应用的全部历史数据，返回各表删除行数（键序对齐 Java LinkedHashMap）。"""
        if not app_name or not app_name.strip():
            raise ValueError("应用名（appName）不能为空，拒绝执行清理")
        app = app_name.strip()
        deleted: dict[str, int] = {}
        with self._sessions() as s:
            # 压测：先删采样（按任务 ID 级联），再删任务
            task_ids = list(s.scalars(
                select(StressTaskModel.id).where(StressTaskModel.app_name == app)))
            deleted["stressSample"] = 0 if not task_ids else s.execute(
                sa_delete(StressSampleModel).where(StressSampleModel.task_id.in_(task_ids))
            ).rowcount
            deleted["stressTask"] = s.execute(
                sa_delete(StressTaskModel).where(StressTaskModel.app_name == app)).rowcount

            # 问题：先删方案（按问题 ID 级联），再删问题
            issue_ids = list(s.scalars(
                select(PerformanceIssueModel.id).where(PerformanceIssueModel.app_name == app)))
            deleted["fixSuggestion"] = 0 if not issue_ids else s.execute(
                sa_delete(FixSuggestionModel).where(FixSuggestionModel.issue_id.in_(issue_ids))
            ).rowcount
            deleted["performanceIssue"] = s.execute(
                sa_delete(PerformanceIssueModel).where(
                    PerformanceIssueModel.app_name == app)).rowcount

            deleted["traceChain"] = s.execute(
                sa_delete(TraceChainModel).where(TraceChainModel.app_name == app)).rowcount
            deleted["jvmMetric"] = s.execute(
                sa_delete(JvmMetricModel).where(JvmMetricModel.app_name == app)).rowcount
            deleted["slowSql"] = s.execute(
                sa_delete(SlowSqlModel).where(SlowSqlModel.app_name == app)).rowcount
            deleted["slowCommand"] = s.execute(
                sa_delete(SlowCommandModel).where(SlowCommandModel.app_name == app)).rowcount
            deleted["appInstance"] = s.execute(
                sa_delete(AppInstanceModel).where(AppInstanceModel.app_name == app)).rowcount
            deleted["dbConn"] = s.execute(
                sa_delete(DbConnModel).where(DbConnModel.app_name == app)).rowcount
            s.commit()

        # AI 知识库：本应用结论（不含全局）；全局结论仅在显式开启时删除
        deleted["aiKnowledge"] = self._knowledge.delete_by_app(app) if self._knowledge else 0
        global_deleted = 0
        if include_global_knowledge:
            global_deleted = self._knowledge.delete_global() if self._knowledge else 0
            # 审计日志：删全局结论影响所有项目，必须可追溯（操作人由 API 层补充记录）
            log.warning("审计: 清理时连带删除全局 AI 结论, appName=%s, aiKnowledgeGlobal=%s",
                        app, global_deleted)
        deleted["aiKnowledgeGlobal"] = global_deleted
        return deleted
