"""FastAPI 应用工厂：阶段 4 AI 域 + 阶段 5 按域绞杀（stress/collector/datagen/auth/source）。

依赖注入走 app.state（与 Java @Configuration Bean 装配等价）：
- auth_props / jwt_service：单管理员 JWT 鉴权（perf.auth.* 语义）；
- ai_service / tools_registry / knowledge：AI 域（阶段 4）；
- stress_repo / collector_repo / datagen_repo / source_repo：SQLAlchemy 各域仓储。

鉴权（对齐 Java WebAuthConfig）：
- JWT 拦截 /api/**（白名单 /api/auth/**、/api/agent/**），X-Perf-Api-Token 并行放行；
- 探针通道独立令牌 X-Perf-Agent-Token（令牌为空时保持开放）；
- 鉴权开关 PERF_AUTH_ENABLED（Java perf.auth.enabled，默认 true）。

LlmClient 由调用方注入（测试注入脚本化假客户端；生产 client_from_env()）。
"""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agents.llm import LlmClient
from server.ai_api import router as ai_router
from server.ai_service import AiAgentService
from server.auth_api import router as auth_router
from server.auth_service import AUTH_FAIL_BODY, AuthProperties, JwtService
from server.capacity_api import router as capacity_router
from server.collector_api import router as collector_router
from server.collector_repo import CollectorRepository
from server.datagen_api import router as datagen_router
from server.datagen_plan_service import DatagenPlanService
from server.datagen_repo import DatagenRepository
from server.datagen_service import DatagenWriteService
from server.dataset_api import router as dataset_router
from server.dataset_service import DatasetLifecycleService
from server.db import database_url, make_session_factory
from server.db_explain import DbExplainService
from server.dbconn_api import router as dbconn_router
from server.dbconn_service import DbConnService
from server.deploy_api import router as deploy_router
from server.deploy_repo import DeployConnRepository
from server.deploy_service import DeployService
from server.endpoint_scanner import EndpointScanner
from server.errors import handle_exception, handle_validation
from server.issue_service import (
    FixPromptGenerator,
    IssueAssociator,
    PerformanceAnalyzer,
    SourceIssueService,
)
from server.issue_verify_service import IssueVerifyService
from server.jvm_alert_api import router as jvm_alert_router
from server.knowledge import KnowledgeService, SqliteKnowledgeStore
from server.metrics_api import router as metrics_router
from server.ptde_repo import PtdeDatasetRepository
from server.report_api import router as report_router
from server.schedule_api import router as schedule_router
from server.schedule_service import (
    StressScheduleRepository,
    StressScheduleService,
)
from server.source_api import router as source_router
from server.source_index import SourceFileIndexer
from server.source_repo import SourceRepository
from server.source_write_service import DataCleanupService, SourceWriteService
from server.stress_api import router as stress_router
from server.stress_repo import StressRepository
from server.stress_service import InlineRunner, StressTaskService
from tools.registry import build_registry


def _jwt_authorized(request: Request, props: AuthProperties,
                    jwt_service: JwtService) -> bool:
    """对齐 JwtAuthInterceptor.preHandle：CI 静态令牌先行，JWT Bearer 其次。"""
    if props.api_token:
        provided = request.headers.get("X-Perf-Api-Token")
        if provided is not None and hmac.compare_digest(
                props.api_token.encode("utf-8"), provided.encode("utf-8")):
            request.state.ci = True
            return True
    auth = request.headers.get("Authorization")
    if auth is not None and auth.startswith("Bearer "):
        subject = jwt_service.verify(auth[7:])
        if subject is not None:
            request.state.user = subject
            return True
    return False


def create_app(llm: LlmClient | None = None, knowledge: KnowledgeService | None = None,
               registry=None, include_knowledge_tool: bool = True,
               stress_repo: StressRepository | None = None,
               collector_repo: CollectorRepository | None = None,
               datagen_repo: DatagenRepository | None = None,
               source_repo: SourceRepository | None = None,
               source_storage_dir: str | None = None,
               data_cleanup: DataCleanupService | None = None,
               db_explain: DbExplainService | None = None,
               dbconn_service: DbConnService | None = None,
               issue_verify: IssueVerifyService | None = None,
               db_url: str | None = None,
               auth: AuthProperties | None = None,
               auth_enabled: bool | None = None,
               stress_runner=None,
    dataset_repo: PtdeDatasetRepository | None = None,
    dataset_pipeline=None,
    dataset_pipeline_factory=None,
    schedule_service=None,
    deploy_conn_repo=None,
    deploy_service=None) -> FastAPI:
    """构建 FastAPI 应用。

    include_knowledge_tool：把 knowledge_search 挂入工具注册表（AI 可检索知识库）。
    各 repo：注入既有仓储（契约测试用种子库）；否则按 db_url/环境变量建库。
    auth：注入完整鉴权配置；或仅以 auth_enabled 覆盖开关（None = 读
    PERF_AUTH_ENABLED，Java perf.auth.enabled 默认 true）。
    stress_runner：压测写路径执行器（None → 内存库用 InlineRunner 进程内执行，
    共享库用 SubprocessRunner 子进程执行——gevent 与 uvicorn 运行时隔离）。
    """
    if registry is None:
        registry = build_registry()
        if include_knowledge_tool and knowledge is not None:
            from tools.knowledge_tools import build_knowledge_tool

            registry["knowledge_search"] = build_knowledge_tool(knowledge)

    app = FastAPI(title="PerformanceTestor Python Server",
                  description="perf-py server 骨架（阶段 4 AI 域 + 阶段 5 按域绞杀）",
                  version="0.3.0")
    app.state.tools_registry = registry
    app.state.knowledge = knowledge if knowledge is not None \
        else KnowledgeService(SqliteKnowledgeStore())
    # P1-2 观测工具接线：collector/stress 仓储与 EXPLAIN 服务需在 AiAgentService
    # 之前就绪（工具闭包引用），并经 setdefault 注入（不覆盖测试注入的桩工具）。
    session_factory = make_session_factory(db_url or database_url())
    resolved_db_url = db_url or database_url()
    app.state.collector_repo = collector_repo if collector_repo is not None \
        else CollectorRepository(session_factory)
    app.state.stress_repo = stress_repo if stress_repo is not None \
        else StressRepository(session_factory)
    app.state.db_explain = db_explain if db_explain is not None else DbExplainService()
    from tools.observability_tools import build_observability_tools

    for _tool in build_observability_tools(
            app.state.collector_repo, app.state.stress_repo, app.state.db_explain):
        registry.setdefault(_tool.name, _tool)
    app.state.ai_service = AiAgentService(llm, registry, app.state.knowledge)
    app.state.dataset_repo = dataset_repo if dataset_repo is not None \
        else PtdeDatasetRepository(session_factory)
    # dataset_pipeline：数据集执行体注入缝（generate/load/verify 三段）。
    # P0-2 真库接线：未显式注入 pipeline/工厂且 PTDE_REALDB_URL 已配置时，
    # 按数据集实例目标库动态装配 build_realdb_pipeline（db_conn 默认连接兜底）；
    # 两者皆缺省保持 UnwiredPipeline 显式失败（不伪装可用）。
    resolved_pipeline_factory = dataset_pipeline_factory
    if resolved_pipeline_factory is None and dataset_pipeline is None \
            and os.environ.get("PTDE_REALDB_URL"):
        from server.dataset_pipeline_factory import realdb_pipeline_factory

        resolved_pipeline_factory = realdb_pipeline_factory(app.state.dataset_repo)
    app.state.dataset_service = DatasetLifecycleService(
        app.state.dataset_repo, pipeline=dataset_pipeline,
        pipeline_factory=resolved_pipeline_factory,
        # P1-3 参数样本导出目录（默认启用，压测 useSampleCsv 注入源）
        sample_dir=os.environ.get("PERF_DATASET_SAMPLE_DIR")
        or str(Path.home() / ".perftestor" / "samples"))
    if stress_runner is not None:
        runner = stress_runner
    elif ":memory:" in resolved_db_url:
        runner = InlineRunner(session_factory)
    else:
        from server.stress_service import SubprocessRunner

        runner = SubprocessRunner(resolved_db_url)
    # T7.3 压测绑定：dataset 查询缝注入（创建任务时快照 READY 数据集）。
    # P1-3 扩展：environmentId（K5 生产红线校验）+ 样本 CSV 摘要（useSampleCsv 注入）。
    def _dataset_lookup(instance_id: str):
        try:
            d = app.state.dataset_service.get_instance(instance_id)
        except ValueError:
            return None
        sample_path, sample_rows = _dataset_sample_info(instance_id)
        return {
            "rowId": int(d["id"]),
            "status": d["status"],
            "definitionVersion": d["definitionVersion"],
            "schemaFingerprint": d["schemaFingerprint"],
            "validationSummary": d["validationSummary"],
            "environmentId": d.get("environmentId"),
            "sampleCsvPath": sample_path,
            "sampleCsvRows": sample_rows,
        }

    def _dataset_sample_info(instance_id: str) -> tuple[str | None, int]:
        """最近一次 manifest 的样本 CSV 摘要（无 manifest/读取失败 → 缺省）。"""
        try:
            rows = app.state.dataset_repo.manifests_for_instance(int(instance_id))
            if not rows:
                return None, 0
            m = app.state.dataset_repo.manifest_to_model(rows[-1])
            return m.sample_csv_path, m.sample_csv_rows or 0
        except Exception:  # noqa: BLE001 — 样本摘要缺失不阻断绑定
            return None, 0

    # P1-3 数据集重置钩子：cleanup（决策挡生产/未知）→ 重建（尽力而为，异常由钩子捕获留痕）
    def _dataset_reset(instance_id: str, phase: str) -> None:
        app.state.dataset_service.cleanup(
            instance_id, env_class="staging_like", operator=f"stress-reset-{phase}")
        app.state.dataset_service.build(instance_id, operator=f"stress-reset-{phase}")

    app.state.stress_service = StressTaskService(
        app.state.stress_repo, runner, dataset_lookup=_dataset_lookup,
        dataset_reset=_dataset_reset)
    # P1-3 孤儿收口：进程重启遗留的 running/pending 任务统一置 failed（防僵尸占用）。
    # 尽力而为：测试/最小建库形态下 stress_task 可能不存在，缺表仅告警不阻断启动。
    try:
        app.state.stress_repo.fail_stale("服务重启，任务已中断")
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "stress orphan sweep skipped (stress_task table unavailable)", exc_info=True)
    app.state.dbconn_service = dbconn_service if dbconn_service is not None \
        else DbConnService(session_factory)
    app.state.datagen_repo = datagen_repo if datagen_repo is not None \
        else DatagenRepository(session_factory)
    app.state.datagen_service = DatagenWriteService(app.state.datagen_repo)
    app.state.datagen_plan_service = DatagenPlanService(
        app.state.datagen_repo, app.state.stress_repo,
        task_service=app.state.datagen_service,
        inline_analyze=":memory:" in resolved_db_url, llm=llm)
    app.state.source_repo = source_repo if source_repo is not None \
        else SourceRepository(session_factory)
    app.state.source_file_indexer = SourceFileIndexer(app.state.source_repo)
    app.state.source_write_service = SourceWriteService(
        app.state.source_repo, app.state.source_file_indexer,
        storage_dir=source_storage_dir, collector_repo=app.state.collector_repo,
        inline_import=":memory:" in resolved_db_url)
    app.state.data_cleanup_service = data_cleanup if data_cleanup is not None \
        else DataCleanupService(session_factory, app.state.knowledge.store)
    # P0-1b/1d：定时调度与 deploy 域（deploy SSH 执行体缺省 paramiko 懒加载）。
    # 仓储与 stress 域同源 session_factory（测试注入内存库时同库可查）。
    app.state.schedule_service = schedule_service if schedule_service is not None \
        else StressScheduleService(
            StressScheduleRepository(session_factory), app.state.stress_service)
    # 调度守护线程仅持久库启动（内存库为测试/冒烟形态，避免空库扫描噪音；
    # 测试经注入 schedule_service 或直接调 run_due 验证触发语义）
    if ":memory:" not in resolved_db_url:
        app.state.schedule_service.start()
    app.state.deploy_conn_repo = deploy_conn_repo if deploy_conn_repo is not None \
        else DeployConnRepository(session_factory)
    app.state.deploy_service = deploy_service if deploy_service is not None \
        else DeployService(app.state.deploy_conn_repo, app.state.source_repo)
    app.state.endpoint_scanner = EndpointScanner(
        app.state.source_repo, app.state.source_write_service,
        app.state.source_file_indexer)
    app.state.llm = llm
    issue_associator = IssueAssociator(
        app.state.source_repo, app.state.source_file_indexer, app.state.source_write_service)
    issue_analyzer = PerformanceAnalyzer(
        app.state.source_repo, llm, app.state.source_file_indexer,
        app.state.source_write_service, app.state.collector_repo, app.state.db_explain)
    issue_prompts = FixPromptGenerator(
        app.state.source_repo, app.state.source_file_indexer, app.state.source_write_service)
    app.state.issue_service = SourceIssueService(
        app.state.source_repo, app.state.source_write_service, issue_associator,
        issue_analyzer, issue_prompts, app.state.stress_repo)
    app.state.issue_verify_service = issue_verify if issue_verify is not None \
        else IssueVerifyService(
            app.state.source_repo, app.state.stress_repo, app.state.stress_service,
            timeout_sec=int(os.environ.get("PERF_VERIFY_TIMEOUT_SEC", "300")),
            inline=":memory:" in resolved_db_url)
    app.state.auth_props = auth if auth is not None \
        else AuthProperties(enabled=auth_enabled)
    app.state.jwt_service = JwtService(app.state.auth_props)
    # collector_api 沿用此二态（探针令牌独立校验，enabled 与 JWT 开关同源）
    app.state.agent_token = app.state.auth_props.agent_token
    app.state.agent_auth_enabled = app.state.auth_props.enabled

    @app.middleware("http")
    async def jwt_guard(request: Request, call_next):
        """对齐 JwtAuthInterceptor：/api/** 拦截，auth/agent 白名单，401 固定体。"""
        props: AuthProperties = app.state.auth_props
        path = request.url.path
        if props.enabled and path.startswith("/api/") \
                and not path.startswith("/api/auth/") \
                and not path.startswith("/api/agent/") \
                and not _jwt_authorized(request, props, app.state.jwt_service):
            return JSONResponse(status_code=401, content=AUTH_FAIL_BODY)
        return await call_next(request)

    app.include_router(auth_router)
    app.include_router(ai_router)
    app.include_router(stress_router)
    app.include_router(collector_router)
    app.include_router(datagen_router)
    app.include_router(dataset_router)
    app.include_router(source_router)
    app.include_router(dbconn_router)
    app.include_router(metrics_router)
    app.include_router(capacity_router)
    app.include_router(jvm_alert_router)
    app.include_router(schedule_router)
    app.include_router(report_router)
    app.include_router(deploy_router)
    # 全局异常处理对齐 Java GlobalExceptionHandler：任何异常 → 200 + code=1 错误包装
    app.add_exception_handler(RequestValidationError, handle_validation)
    app.add_exception_handler(Exception, handle_exception)
    return app
