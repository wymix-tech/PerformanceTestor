"""source 域 router（阶段 5 · 步骤 5 读路径；步骤 9 写路径）：契约对齐 SourceController。

读端点：
- GET /api/source/repos?limit                    仓库列表（create_time 倒序，1..100）
- GET /api/source/repos/{repoId}/files?prefix&limit  仓库文件列表（path 前缀过滤，升序）
- GET /api/source/issues?appName&issueType&status&limit  问题列表（create_time 倒序，1..200）
- GET /api/source/issues/{issueId}               问题详情（含方案列表；缺失 → code=1「问题不存在」）
- GET /api/source/recent-slow-sqls?app&limit     最近 7 天慢 SQL（cost_ms 倒序，1..50）
- GET /api/source/suggestions/{suggestionId}     修复方案详情（缺失 → code=1「方案不存在」）
- GET /api/source/repos/{repoId}/branches        分支列表（git 专用，ls-remote 直连）
- GET /api/source/files/{fileId}/content         文件内容（懒加载，磁盘仓库根路径定位）

写端点（步骤 9 收编）：
- POST /repos/zip|git|local                       三通道导入（异步，2 线程池；导入即绑定应用）
- POST /repos/{repoId}/resync                     重新同步（同步执行，返回索引文件数）
- POST /repos/git-branches                        远端分支枚举（导入前用，ls-remote 直连）
- POST /repos/{repoId}/branch                     切换分支（远端校验 → 更新 → resync）
- POST /repos/{repoId}/bind-app                   绑定应用（空串 = 解绑）
- POST /repos/{repoId}/cleanup-data               项目历史数据清理（全局结论需二次确认）

错误包装对齐 Java GlobalExceptionHandler：IllegalArgumentException（ValueError）→
code=1 原样文案；IllegalStateException（RuntimeError）→ "服务器内部错误: " 前缀。
问题创建与验证/大模型分析（SourceIssueService）仍由 Java 承担，随后续步骤收编。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, File, Form, Request, UploadFile

from server.source_repo import SourceRepository, slow_sql_to_camel

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/source")


def _ok(data) -> dict:
    return {"code": 0, "message": "success", "data": data}


def _err(message: str) -> dict:
    return {"code": 1, "message": message, "data": None}


def _repo(request: Request) -> SourceRepository:
    return request.app.state.source_repo


def _write(request: Request):
    return request.app.state.source_write_service


async def _body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001 缺 body 时按空 Map 处理（cleanup-data body 可选）
        return {}


def _run(request: Request, fn) -> dict:
    """服务调用统一包装：ValueError → 原样文案；RuntimeError → 内部错误前缀。"""
    try:
        return _ok(fn())
    except ValueError as exc:
        return _err(str(exc))
    except RuntimeError as exc:
        return _err(f"服务器内部错误: {exc}")


@router.get("/repos")
def repos(request: Request, limit: int = 50) -> dict:
    return _ok([r.to_camel() for r in _repo(request).repos(limit)])


@router.get("/repos/{repo_id}/files")
def files(repo_id: int, request: Request, prefix: str | None = None,
          limit: int = 5000) -> dict:
    return _ok([f.to_camel() for f in _repo(request).files(repo_id, prefix, limit)])


@router.get("/issues")
def issues(request: Request, app_name: str | None = None,
           issue_type: str | None = None, status: str | None = None,
           limit: int = 100) -> dict:
    return _ok([i.to_camel() for i in
                _repo(request).issues(app_name, issue_type, status, limit)])


@router.get("/issues/{issue_id}")
def issue_detail(issue_id: int, request: Request) -> dict:
    """对齐 Java getIssueDetail：问题缺失抛 IllegalArgumentException → 全局错误包装。"""
    repo = _repo(request)
    issue = repo.issue_by_id(issue_id)
    if issue is None:
        return _err(f"问题不存在: {issue_id}")
    return _ok({"issue": issue.to_camel(),
                "suggestions": [s.to_camel() for s in repo.suggestions_by_issue(issue_id)]})


@router.get("/recent-slow-sqls")
def recent_slow_sqls(request: Request, app: str | None = None, limit: int = 20) -> dict:
    return _ok([slow_sql_to_camel(r) for r in _repo(request).recent_slow_sqls(app, limit)])


@router.get("/suggestions/{suggestion_id}")
def suggestion(suggestion_id: int, request: Request) -> dict:
    row = _repo(request).suggestion_by_id(suggestion_id)
    if row is None:
        return _err(f"方案不存在: {suggestion_id}")
    return _ok(row.to_camel())


# ---------------------------------------------------------------- 写路径（步骤 9）


@router.post("/repos/zip")
async def import_zip(request: Request,
                     repo_name: str = Form(..., alias="repoName"),
                     app_name: str | None = Form(None, alias="appName"),
                     file: UploadFile | None = File(None)) -> dict:
    """zip 上传导入（appName 导入即绑定应用）。"""
    data = await file.read() if file is not None else b""
    if not data:
        return _err("请选择要上传的 zip 文件")
    return _run(request, lambda: _write(request).start_import(
        repo_name, "zip",
        _write(request).save_zip(data, file.filename if file else None),
        None, None, None, app_name))


@router.post("/repos/git")
async def import_git(request: Request) -> dict:
    """git 地址导入（可选分支、私有仓库凭据；导入即绑定应用）。"""
    body = await request.json()
    url = body.get("gitUrl")
    if not url or not url.strip():
        return _err("git 地址不能为空")
    return _run(request, lambda: _write(request).start_import(
        body.get("repoName"), "git", url, body.get("branch"),
        body.get("gitUser"), body.get("gitToken"), body.get("appName")))


@router.post("/repos/local")
async def import_local(request: Request) -> dict:
    """本地目录导入（appName 导入即绑定应用）。"""
    body = await request.json()
    path = body.get("localPath")
    if not path or not path.strip():
        return _err("本地路径不能为空")
    return _run(request, lambda: _write(request).start_import(
        body.get("repoName"), "local", path, None, None, None, body.get("appName")))


@router.post("/repos/git-branches")
async def remote_branches(request: Request) -> dict:
    """远端分支枚举（导入前使用，git ls-remote 直连，支持私有仓库凭据）。"""
    body = await request.json()
    return _run(request, lambda: _write(request).list_remote_branches(
        body.get("gitUrl"), body.get("gitUser"), body.get("gitToken")))


@router.post("/repos/{repo_id}/resync")
def resync(repo_id: int, request: Request) -> dict:
    """重新同步仓库：清空副本 → 重新拉取源码 → 重建索引（同步执行）。"""
    return _run(request, lambda: {"repoId": repo_id,
                                  "indexed": _write(request).resync(repo_id)})


@router.get("/repos/{repo_id}/branches")
def branches(repo_id: int, request: Request) -> dict:
    """分支列表（git 仓库专用，ls-remote 直连枚举远端）。"""
    return _run(request, lambda: _write(request).list_branches(repo_id))


@router.post("/repos/{repo_id}/branch")
async def switch_branch(repo_id: int, request: Request) -> dict:
    """切换分支：更新配置并重新同步（fetch + checkout + 重建索引）。"""
    body = await request.json()
    branch = body.get("branch")
    return _run(request, lambda: {"repoId": repo_id,
                                  "branch": branch if branch else "",
                                  "indexed": _write(request).switch_branch(repo_id, branch)})


@router.post("/repos/{repo_id}/bind-app")
async def bind_app(repo_id: int, request: Request) -> dict:
    """绑定应用（空串 = 解绑）。"""
    body = await request.json()
    return _run(request, lambda: {"repoId": repo_id,
                                  "appName": _write(request).bind_app(repo_id,
                                                                      body.get("appName"))})


@router.post("/repos/{repo_id}/cleanup-data")
async def cleanup_data(repo_id: int, request: Request) -> dict:
    """清理项目历史数据（按绑定 appName 级联）；删全局 AI 结论需 confirmAppName 二次确认。"""
    body = await _body(request)
    repo = request.app.state.source_repo.repo_by_id(repo_id)
    if repo is None:
        return _err(f"仓库不存在: {repo_id}")
    app_name = repo.app_name
    if not app_name or not app_name.strip():
        return _err("项目未绑定应用（appName），无法定位历史数据，请先在项目列表绑定应用")
    # Boolean.parseBoolean 语义：字符串忽略大小写等于 "true"
    include_global = str(body.get("includeGlobalKnowledge", "false")).lower() == "true"
    if include_global:
        confirm = body.get("confirmAppName")
        confirm = "" if confirm is None else str(confirm).strip()
        if app_name != confirm:
            return _err("开启全局知识清理需二次确认：请求体 confirmAppName 必须与项目绑定的 "
                        "appName 完全一致")
        log.warning("审计: 用户请求清理全局 AI 结论, repoId=%s, appName=%s, operator=%s",
                    repo_id, app_name,
                    "ci" if getattr(request.state, "ci", False)
                    else getattr(request.state, "user", "unknown"))
    deleted = request.app.state.data_cleanup_service.cleanup_by_app_name(
        app_name, include_global)
    return _ok({"repoId": repo_id, "appName": app_name,
                "includeGlobalKnowledge": include_global, "deleted": deleted})


# ---------------------------------------------------------------- 补齐读端点


@router.get("/files/{file_id}/content")
def file_content(file_id: int, request: Request) -> dict:
    """文件内容（懒加载；磁盘仓库根路径定位，对齐 SourceController.content）。"""
    repo_svc = _repo(request)
    sf = repo_svc.file_by_id(file_id)
    if sf is None:
        return _err(f"文件不存在: {file_id}")
    repo = repo_svc.repo_by_id(sf.repo_id)
    if repo is None:
        return _err(f"仓库不存在: {sf.repo_id}")
    root = request.app.state.source_write_service.repo_root_dir(repo.id)
    try:
        return _ok({"relativePath": sf.relative_path, "language": sf.language,
                    "content": request.app.state.source_file_indexer.read_content(root, sf)})
    except ValueError as exc:
        return _err(str(exc))
    except OSError as exc:
        return _err(f"服务器内部错误: 读取文件失败: {exc}")
