import http from './http'

// 各模块 API 统一封装，路径与 perf-server Controller 完全对齐
// 后端统一返回 { code, message, data }，http 拦截器已解包为该对象

export const authApi = {
  // 登录（成功返回 token；业务失败 code=401 不触发 http 拦截器跳转）
  login: (data: { username: string; password: string }) => http.post('/auth/login', data),
  // 会话探测（校验当前 token 是否有效）
  me: () => http.get('/auth/me')
}

export const appApi = {
  // 应用实例列表（在线优先，总览/应用切换用）
  list: () => http.get('/apps')
}

export const jvmApi = {
  // JVM 指标时序数据（start/end 支持 epoch 毫秒或 ISO，默认最近 1 小时）
  metrics: (params: { app?: string; start?: string | number; end?: string | number; limit?: number }) =>
    http.get('/jvm/metrics', { params })
}

export const traceApi = {
  // 调用链列表（时间倒序）
  list: (params: { app?: string; method?: string; start?: string | number; end?: string | number; limit?: number }) =>
    http.get('/traces', { params }),
  // 调用链详情（含 traceJson 调用树）
  detail: (traceId: string) => http.get(`/traces/${traceId}`)
}

export const slowSqlApi = {
  // 慢 SQL 列表（耗时倒序）
  list: (params: { app?: string; start?: string | number; end?: string | number; minCostMs?: number; limit?: number }) =>
    http.get('/slow-sqls', { params })
}

export const slowCommandApi = {
  // 中间件慢命令列表（Redis / ES / MQ，耗时倒序；type 为空表示全部）
  list: (params: { app?: string; type?: string; start?: string | number; end?: string | number; minCostMs?: number; limit?: number }) =>
    http.get('/slow-commands', { params })
}

export const hotspotApi = {
  // 方法级 CPU 热点排行（命中次数降序，最近 1 小时）
  list: (params: { app?: string; start?: string | number; end?: string | number; limit?: number }) =>
    http.get('/hotspots', { params })
}

export const stressApi = {
  // 创建并启动压测任务（异步执行）
  create: (data: Record<string, unknown>) => http.post('/stress/tasks', data),
  // 任务列表
  list: (params?: { status?: string; limit?: number }) => http.get('/stress/tasks', { params }),
  // 周期采样（实时图表）
  samples: (id: number, limit = 500) => http.get(`/stress/tasks/${id}/samples`, { params: { limit } }),
  // 压测报告（含统计/分档采样点/容量评估结果）
  report: (id: number) => http.get(`/stress/tasks/${id}/report`),
  // 历史趋势：同应用 + 同目标 URL 的最近完成任务指标序列（报告页折线图用）
  trend: (params: { appName: string; url: string; limit?: number }) =>
    http.get('/stress/trend', { params }),
  // 性能回归门禁（CI 用）：同步等待压测完成并比对基线，返回 { passed, summary, ... }
  gate: (data: Record<string, unknown>, params?: { baselineTaskId?: number; timeoutSec?: number; failOnNoBaseline?: boolean; webhook?: string }) =>
    http.post('/stress/gate', data, { params })
}

export const jvmAlertApi = {
  // JVM 阈值告警规则列表（appName 为空查全部）
  list: (appName?: string, limit = 100) =>
    http.get('/jvm-alert-rules', { params: { appName, limit } }),
  // 支持的指标键（下拉选项）
  metrics: () => http.get('/jvm-alert-rules/metrics'),
  // 新增规则
  add: (data: Record<string, unknown>) => http.post('/jvm-alert-rules', data),
  // 更新规则
  update: (id: number, data: Record<string, unknown>) => http.put(`/jvm-alert-rules/${id}`, data),
  // 删除规则
  remove: (id: number) => http.delete(`/jvm-alert-rules/${id}`),
  // 启用/停用
  setEnabled: (id: number, enabled: boolean) =>
    http.post(`/jvm-alert-rules/${id}/enabled`, null, { params: { enabled } }),
  // 手动触发一次判定（返回 triggered 表示是否超阈值）
  check: (id: number) => http.post(`/jvm-alert-rules/${id}/check`)
}

export const reportApi = {
  /**
   * 报告导出/打印页 URL（文件下载走 Blob，不能直接走 JSON 拦截器）
   * 说明：导出接口返回裸文件流（非 ApiResponse 包壳），需带 token 访问，
   * 故打印页用 window.open 直开、文件下载用 responseType:'blob'。
   */
  exportUrl: (taskId: number, format: 'html' | 'pdf' | 'doc', printMode = false) =>
    `/api/reports/export?stressTaskId=${taskId}&format=${format}&printMode=${printMode}`
}

export const scheduleApi = {
  // 定时压测调度列表
  list: () => http.get('/stress/schedules'),
  // 创建调度（task 为压测任务模板）
  create: (data: Record<string, any>) => http.post('/stress/schedules', data),
  // 启停调度
  toggle: (id: number, enabled: boolean) =>
    http.put(`/stress/schedules/${id}/enabled`, null, { params: { enabled } }),
  // 删除调度
  remove: (id: number) => http.delete(`/stress/schedules/${id}`),
  // 基线对比（可选 taskId，默认上次触发任务）
  diff: (id: number, taskId?: number) =>
    http.get(`/stress/schedules/${id}/diff`, { params: taskId ? { taskId } : {} })
}

export const dataGenApi = {
  // 启动灌数据任务（dryRun=true 仅校验预检；tables 支持联表表组）
  create: (data: Record<string, unknown>) => http.post('/datagen/tasks', data),
  // 任务列表
  list: (limit = 50) => http.get('/datagen/tasks', { params: { limit } }),
  // 读取目标库表清单
  tables: (data: { connId?: number; dbUrl: string; dbUser: string; dbPassword: string }) =>
    http.post('/datagen/tables', data),
  // 读取目标表列结构（含枚举建议值与疑似枚举标记）
  columns: (data: { connId?: number; dbUrl: string; dbUser: string; dbPassword: string; tableName: string }) =>
    http.post('/datagen/columns', data),
  // 语义建议查询（hintType=value/relation；status=pending 待确认）
  suggestions: (params?: { repoId?: number; tableName?: string; hintType?: string; status?: string }) =>
    http.get('/datagen/suggestions', { params }),
  // 建议采纳/忽略
  adopt: (id: number, adopt = true) => http.post(`/datagen/suggestions/${id}/adopt`, null, { params: { adopt } })
}

/**
 * 接口驱动数据方案：按压测接口分析源码调用链，推导出待造表、数据构成与行数。
 * 方案落库后可回看（推荐理由带 文件:行号 证据）与重复执行。
 */
export const dataGenPlanApi = {
  // 按接口分析并生成方案（异步分析，立即返回已落库方案，前端轮询 detail 取结果）
  analyze: (data: Record<string, unknown>) => http.post('/datagen/plans/analyze', data),
  // 方案列表（按仓库 + 时间倒序）
  list: (params?: { repoId?: number; status?: string; limit?: number }) =>
    http.get('/datagen/plans', { params }),
  // 方案详情（表清单 / 规则 / 证据 / 告警）
  detail: (id: number) => http.get(`/datagen/plans/${id}`),
  // 方案采纳/放弃
  adopt: (id: number, adopt = true) =>
    http.post(`/datagen/plans/${id}/adopt`, null, { params: { adopt } }),
  // 按方案执行灌数（dryRun=true 仅试运行；conn 用于方案未指定连接时按当前选定值执行）
  execute: (id: number, dryRun = true, conn?: { connId?: number; dbUrl?: string }) =>
    http.post(`/datagen/plans/${id}/execute`, conn || null, { params: { dryRun } }),
  // 删除方案
  remove: (id: number) => http.delete(`/datagen/plans/${id}`)
}

export const deployApi = {
  // SSH 连接列表
  conns: (limit = 50) => http.get('/deploy/conns', { params: { limit } }),
  // 新增 SSH 连接
  addConn: (data: Record<string, unknown>) => http.post('/deploy/conns', data),
  // 测试 SSH 连接连通性（不落库，新增前探活）
  testConn: (data: Record<string, unknown>) => http.post('/deploy/conns/test', data),
  // 按已保存连接 ID 测试连通性（凭据不出服务端）
  testConnById: (id: number) => http.post(`/deploy/conns/${id}/test`),
  // 删除 SSH 连接
  delConn: (id: number) => http.delete(`/deploy/conns/${id}`),
  // 平台侧编译被测项目（返回 jarPath）
  build: (data: { repoId: number; module?: string }) => http.post('/deploy/build', data),
  // 上传编译包并远程启动（agent 自动探测挂载）
  start: (data: { connId: number; jarPath: string; agentPath?: string; appPort?: number }) =>
    http.post('/deploy/start', data),
  // 远程应用状态（进程/健康检查/日志尾部）
  status: (connId: number, appPort = 8081) => http.get('/deploy/status', { params: { connId, appPort } })
}

export const capacityApi = {
  // 对压测任务执行容量评估（幂等，结果落库）
  analyze: (data: { taskId: number; targetP95Ms?: number; app?: string }) =>
    http.post('/capacity/analyze', data),
  // 实时扩容与 JVM 配置建议
  advice: (params?: { app?: string }) => http.get('/capacity/advice', { params })
}

export const sourceApi = {
  // 源码仓库列表
  repos: (limit = 50) => http.get('/source/repos', { params: { limit } }),
  // 扫描仓库 HTTP 接口清单（压测中心选项目自动出接口）
  endpoints: (repoId: number) => http.get(`/source/repos/${repoId}/endpoints`),
  // 重新同步仓库：重新拉取源码并重建索引（源码更新后调用）
  resync: (repoId: number) => http.post(`/source/repos/${repoId}/resync`),
  // 清理项目历史数据：删除该应用关联的压测/慢SQL/监控/问题等全部数据（源码与项目本身保留）
  cleanupData: (repoId: number) => http.post(`/source/repos/${repoId}/cleanup-data`),
  // 分支列表（git 仓库）
  branches: (repoId: number) => http.get(`/source/repos/${repoId}/branches`),
  // 切换分支（git 仓库，切换后自动重新同步）
  switchBranch: (repoId: number, branch: string) => http.post(`/source/repos/${repoId}/branch`, { branch }),
  // zip 上传导入（multipart；appName 导入即绑定应用）
  importZip: (repoName: string, file: File, appName?: string) => {
    const fd = new FormData()
    fd.append('repoName', repoName)
    if (appName) fd.append('appName', appName)
    fd.append('file', file)
    return http.post('/source/repos/zip', fd)
  },
  // git 地址导入（appName 导入即绑定应用）
  importGit: (data: { repoName?: string; gitUrl: string; branch?: string; gitUser?: string; gitToken?: string; appName?: string }) =>
    http.post('/source/repos/git', data),
  // 枚举远端分支（导入前，无需 clone；支持私有仓库凭据）
  remoteBranches: (gitUrl: string, gitUser?: string, gitToken?: string) =>
    http.post('/source/repos/git-branches', { gitUrl, gitUser, gitToken }),
  // 本地目录导入（appName 导入即绑定应用）
  importLocal: (data: { repoName?: string; localPath: string; appName?: string }) => http.post('/source/repos/local', data),
  // 仓库文件树
  files: (repoId: number, params?: { prefix?: string; limit?: number }) =>
    http.get(`/source/repos/${repoId}/files`, { params }),
  // 文件内容
  fileContent: (fileId: number) => http.get(`/source/files/${fileId}/content`),
  // 性能问题列表
  issues: (params?: { appName?: string; issueType?: string; status?: string; limit?: number }) =>
    http.get('/source/issues', { params }),
  // 问题详情（含方案列表）
  issueDetail: (id: number) => http.get(`/source/issues/${id}`),
  // 补绑源码仓库并自动定位（压测问题分析前置）
  bindIssueRepo: (issueId: number, repoId: number) =>
    http.post(`/source/issues/${issueId}/bind-repo`, null, { params: { repoId } }),
  // 执行大模型分析（生成根因 + 多方案）
  analyze: (id: number) => http.post(`/source/issues/${id}/analyze`),
  // 选定方案并生成修复提示词
  selectSuggestion: (issueId: number, suggestionId: number) =>
    http.post(`/source/issues/${issueId}/suggestions/${suggestionId}/select`),
  // 从慢 SQL 一键创建问题（关联源码）
  fromSlowSql: (params: { slowSqlId: number; repoId: number }) =>
    http.post('/source/issues/from-slow-sql', null, { params }),
  // 从热点方法创建问题
  fromHotMethod: (params: { className: string; methodName: string; repoId: number; appName?: string }) =>
    http.post('/source/issues/from-hot-method', null, { params }),
  // 从压测任务创建问题（可指定归属应用）
  fromStress: (params: { stressTaskId: number; repoId: number; appName?: string }) =>
    http.post('/source/issues/from-stress', null, { params }),
  // 一键验证修复效果：异步发起重放压测，立即返回「进行中」结论
  verifyIssue: (issueId: number) => http.post(`/source/issues/${issueId}/verify`),
  // 查询最近一次验证结论（含进行中状态，前端据此轮询取最终结果）
  verifyStatus: (issueId: number) => http.get(`/source/issues/${issueId}/verify`),
  // 补绑压测场景快照（非压测来源问题建立验证基线）
  bindIssueStress: (issueId: number, stressTaskId: number) =>
    http.post(`/source/issues/${issueId}/bind-stress`, null, { params: { stressTaskId } }),
  // 最近慢 SQL（源码页一键建问题用）
  recentSlowSqls: (params?: { app?: string; limit?: number }) =>
    http.get('/source/recent-slow-sqls', { params })
}

export const aiApi = {
  // AI 对话（Function Calling，服务端无 Key 时降级规则模式）
  chat: (data: { sessionId?: string; message: string }) => http.post('/ai/chat', data),
  // 清空会话
  clearSession: (sessionId: string) => http.delete(`/ai/session/${sessionId}`),
  // 知识库检索（FULLTEXT ngram 中文检索；query 为空时返回最近结论）
  knowledge: (params?: { query?: string; app?: string; limit?: number }) =>
    http.get('/ai/knowledge', { params })
}

export const dbConnApi = {
  // 数据库连接配置列表（慢 SQL 分析 EXPLAIN 用）
  list: () => http.get('/dbconns'),
  // 保存连接
  save: (data: { connName: string; host?: string; appName?: string; dbUrl: string; username: string; password: string; isDefault?: number }) =>
    http.post('/dbconns', data),
  // 删除连接
  remove: (id: number) => http.delete(`/dbconns/${id}`),
  // 设为默认
  setDefault: (id: number) => http.post(`/dbconns/${id}/default`),
  // 测试连接（可选携带 SQL 试跑 EXPLAIN）
  test: (data: { dbUrl: string; username: string; password: string; sqlText?: string }) =>
    http.post('/dbconns/test', data)
}

export default { appApi, jvmApi, jvmAlertApi, traceApi, slowSqlApi, slowCommandApi, stressApi, reportApi, scheduleApi, dataGenApi, dataGenPlanApi, capacityApi, sourceApi, aiApi, dbConnApi }
