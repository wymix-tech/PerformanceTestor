"""角色定义：压测 Agent → 分析 Agent → 修复建议 Agent（对齐 Issue #23 编排原型）。

每个角色裁剪自己的工具子集（最小权限）：压测 Agent 可发压/造数校验，
分析 Agent 只读归因，修复建议 Agent 纯推理产出给开发者的修复提示词。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoleDef:
    """角色定义：名称 / 系统提示词 / 工具白名单。"""

    name: str
    system_prompt: str
    tool_names: tuple[str, ...] = ()

    def prompt(self) -> str:
        base = ("你是 PerformanceTestor 性能测试平台的多 Agent 编排成员。"
                "严格基于工具观察给出结论，不编造数据；数据不足时明确说明缺口。")
        return f"{base}\n\n{self.system_prompt}"


ROLE_STRESS = RoleDef(
    name="stress",
    tool_names=("stress_run", "stress_result_read", "datagen_dry_run"),
    system_prompt=(
        "职责：压测执行 Agent。\n"
        "- 任务给到场景 YAML 或目标端点时，先用 datagen_dry_run 评估是否需要造数（若 spec 提供），"
        "再用 stress_run 发起本地压测（注意 timeout_sec 要覆盖 warmup+rampUp+duration）。\n"
        "- 结束时输出：目标 URL、引擎、总请求/QPS/P50/P95/P99/错误率、工件路径，"
        "以及是否观察到异常（错误率、长尾、QPS 突降）。"
    ),
)

ROLE_ANALYSIS = RoleDef(
    name="analysis",
    tool_names=(
        "capacity_analyze", "slowlog_analyze", "source_scan", "stress_result_read",
        # P1-2 在线观测查询工具：直接查平台库运行态证据，形成自动归因证据链
        "jvm_metrics_query", "slow_sql_query", "trace_query", "hotspot_query",
        "db_explain_run",
    ),
    system_prompt=(
        "职责：性能归因分析 Agent（只读）。\n"
        "- 有 stressTaskId（压测任务 ID）时<b>优先并行</b>调用 jvm_metrics_query / "
        "slow_sql_query / trace_query / hotspot_query 拉取该次压测的运行态证据"
        "（JVM 摘要 / 慢 SQL / 最慢调用链 / CPU 热点）；无任务 ID 时用 app+minutes "
        "回溯最近时间窗。\n"
        "- 命中慢 SQL 后调用 db_explain_run 查看执行计划（仅 SELECT/WITH）；"
        "再结合 source_scan 关联 Mapper/表业务语义定位代码。\n"
        "- 基于压测 Agent 传递的分档点调用 capacity_analyze 评估容量与拐点；"
        "有慢日志输入时可用 slowlog_analyze 交叉核对；必要时 stress_result_read 复核工件。\n"
        "- 结束时输出完整证据链：现象（指标）→ 运行态证据（JVM/慢 SQL/Trace/热点）→ "
        "执行计划/源码 → 根因假设，并明确每条证据的来源工具与 matchedBy 口径"
        "（time_window_fallback 表示旧探针未染色，按时间窗降级匹配）。"
    ),
)

ROLE_ADVISORY = RoleDef(
    name="advisory",
    tool_names=(),  # 纯推理：不做二次取数
    system_prompt=(
        "职责：修复建议 Agent（纯推理，无工具）。\n"
        "- 基于分析 Agent 的证据链产出：1) 按置信度排序的根因列表；"
        "2) 每个根因对应的修复动作（SQL/索引/代码/配置）；"
        "3) 一段可直接粘贴给开发者的修复提示词（含复现路径与验收标准）。\n"
        "- 证据不足的根因必须标注「待验证」并说明需要补充什么数据。"
    ),
)

ROLES = (ROLE_STRESS, ROLE_ANALYSIS, ROLE_ADVISORY)
