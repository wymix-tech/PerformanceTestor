"""B7 续批次 3：selfCheck 端点抽检回调接线测试（B6 §7F 口径）。

- ``DatagenWriteService.self_check_probe(repo_id)`` 适配器：selfCheck 端点
  抽检能力 → ``validate_db_ready(endpoint_probe=...)`` 协议回调
  ``() -> {"checked": n, "nonEmpty": m}``（OfflinePipeline 构造注入）；
- legacy selfCheck 行为零改动：``_self_check`` 报告形状（endpoints/
  nonEmptyRate/warning）回归锁定；``datagen/**`` 零修改；
- 语义与 legacy 对齐：仓库未绑定应用 → checked=0（验证层 WARN「无端点可
  查」）；单端点失败计未命中，抽检从不阻断。
"""

from __future__ import annotations

import httpx

from server.datagen_service import DatagenWriteService

# ---------------------------------------------------------------- 替身


class _RepoRow:
    def __init__(self, app_name: str | None):
        self.app_name = app_name


class _AppRow:
    def __init__(self, host: str | None, port: int | None):
        self.app_host = host
        self.app_port = port


class _StubRepo:
    """find_repo / find_app_instance 替身（selfCheck 跨域读取缝）。"""

    def __init__(self, repo_row=None, app_row=None):
        self._repo_row = repo_row
        self._app_row = app_row

    def find_repo(self, repo_id):
        return self._repo_row

    def find_app_instance(self, app_name):
        return self._app_row


class _Resp:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


_ENDPOINTS = [
    {"httpMethod": "GET", "path": "/api/orders"},
    {"httpMethod": "GET", "path": "/api/users"},
    {"httpMethod": "GET", "path": "/api/items"},
]


def _service(repo: _StubRepo) -> DatagenWriteService:
    return DatagenWriteService(repo)  # type: ignore[arg-type]


# ---------------------------------------------------------------- probe 适配器


def test_probe_counts_checked_and_nonempty(monkeypatch):
    repo = _StubRepo(_RepoRow("demo-app"), _AppRow("127.0.0.1", 8080))
    svc = _service(repo)
    monkeypatch.setattr(
        svc, "_scan_get_endpoints", lambda repo_id: list(_ENDPOINTS))
    responses = iter([
        _Resp(200, "payload"),  # 非空（len > 2）
        _Resp(200, "content"),  # 非空
        _Resp(200, "[]"),       # len<=2 → 视为空（legacy 口径一致）
    ])
    monkeypatch.setattr(httpx, "get",
                        lambda url, timeout: next(responses))
    assert svc.self_check_probe(7)() == {"checked": 3, "nonEmpty": 2}


def test_probe_endpoint_failure_counted_not_raised(monkeypatch):
    """单端点失败计未命中，抽检从不阻断（selfCheck 尽力而为口径）。"""
    repo = _StubRepo(_RepoRow("demo-app"), _AppRow("127.0.0.1", 8080))
    svc = _service(repo)
    monkeypatch.setattr(
        svc, "_scan_get_endpoints", lambda repo_id: list(_ENDPOINTS))

    def boom(url, timeout):
        raise ConnectionError("endpoint down")

    monkeypatch.setattr(httpx, "get", boom)
    assert svc.self_check_probe(7)() == {"checked": 3, "nonEmpty": 0}


def test_probe_unbound_app_zero_checked(monkeypatch):
    """仓库未绑定应用 / 无实例信息 / 无 GET 端点 → checked=0
    （validate_db_ready 按「无端点可查」WARN——legacy 跳过语义的映射）。"""
    svc = _service(_StubRepo(None, _AppRow("h", 1)))
    assert svc.self_check_probe(7)() == {"checked": 0, "nonEmpty": 0}
    svc = _service(_StubRepo(_RepoRow(""), _AppRow("h", 1)))
    assert svc.self_check_probe(7)() == {"checked": 0, "nonEmpty": 0}
    svc = _service(_StubRepo(_RepoRow("demo"), None))
    assert svc.self_check_probe(7)() == {"checked": 0, "nonEmpty": 0}
    svc = _service(_StubRepo(_RepoRow("demo"), _AppRow("h", None)))
    assert svc.self_check_probe(7)() == {"checked": 0, "nonEmpty": 0}
    # 无 GET 端点可抽检
    svc = _service(_StubRepo(_RepoRow("demo"), _AppRow("h", 1)))
    monkeypatch.setattr(svc, "_scan_get_endpoints", lambda repo_id: [])
    assert svc.self_check_probe(7)() == {"checked": 0, "nonEmpty": 0}


# ---------------------------------------------------------------- legacy 行为回归锁定


def test_self_check_report_shape_unchanged(monkeypatch):
    """``_self_check`` 重构后报告形状零变化（endpoints/nonEmptyRate/warning）。"""
    repo = _StubRepo(_RepoRow("demo-app"), _AppRow("127.0.0.1", 8080))
    svc = _service(repo)
    monkeypatch.setattr(
        svc, "_scan_get_endpoints", lambda repo_id: list(_ENDPOINTS))
    monkeypatch.setattr(
        httpx, "get",
        lambda url, timeout: _Resp(200, "payload"))
    report = svc._self_check({"repoId": 7}, _RepoRow("demo-app"))
    assert report is not None
    assert report["nonEmptyRate"] == 1.0
    assert [i["path"] for i in report["endpoints"]] == [
        "/api/orders", "/api/users", "/api/items"]
    assert all(i["status"] == 200 and i["nonEmpty"] for i in report["endpoints"])
    assert "warning" not in report  # 有命中 → 无告警


def test_self_check_zero_hit_warning_unchanged(monkeypatch):
    repo = _StubRepo(_RepoRow("demo-app"), _AppRow("127.0.0.1", 8080))
    svc = _service(repo)
    monkeypatch.setattr(
        svc, "_scan_get_endpoints", lambda repo_id: list(_ENDPOINTS))
    monkeypatch.setattr(
        httpx, "get", lambda url, timeout: _Resp(503, ""))
    report = svc._self_check({"repoId": 7}, _RepoRow("demo-app"))
    assert report["nonEmptyRate"] == 0.0
    assert report["warning"] == "数据未命中业务谓词或服务不可达，压测结果可能失真"


def test_self_check_skip_when_unbound():
    """仓库未绑定应用 → None（跳过，legacy 既有口径）。"""
    svc = _service(_StubRepo(None, None))
    assert svc._self_check({"repoId": 7}, None) is None
    assert svc._self_check({"repoId": 7}, _RepoRow("")) is None


def test_probe_contract_matches_validate_db_ready_protocol():
    """probe 回调产出符合 validate_db_ready 的 endpoint_probe 协议形态。"""
    repo = _StubRepo(_RepoRow("demo-app"), _AppRow("127.0.0.1", 8080))
    svc = _service(repo)
    probe = svc.self_check_probe(7)
    result = probe()
    assert isinstance(result, dict)
    assert set(result) == {"checked", "nonEmpty"}
    assert all(isinstance(v, int) for v in result.values())
