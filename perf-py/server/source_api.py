"""source 域 router（阶段 5 · 步骤 5）：/api/source DB 读端点契约对齐 SourceController。

- GET /api/source/repos?limit                    仓库列表（create_time 倒序，1..100）
- GET /api/source/repos/{repoId}/files?prefix&limit  仓库文件列表（path 前缀过滤，升序）
- GET /api/source/issues?appName&issueType&status&limit  问题列表（create_time 倒序，1..200）
- GET /api/source/issues/{issueId}               问题详情（含方案列表；缺失 → code=1「问题不存在」）
- GET /api/source/recent-slow-sqls?app&limit     最近 7 天慢 SQL（cost_ms 倒序，1..50）
- GET /api/source/suggestions/{suggestionId}     修复方案详情（缺失 → code=1「方案不存在」）

写路径（导入/resync/分支/绑定/清理/问题创建与验证/大模型分析）暂由 Java 承担；
文件内容接口依赖磁盘仓库根路径管理，随写路径一并收编。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from server.source_repo import SourceRepository, slow_sql_to_camel

router = APIRouter(prefix="/api/source")


def _ok(data) -> dict:
    return {"code": 0, "message": "success", "data": data}


def _err(message: str) -> dict:
    return {"code": 1, "message": message, "data": None}


def _repo(request: Request) -> SourceRepository:
    return request.app.state.source_repo


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
