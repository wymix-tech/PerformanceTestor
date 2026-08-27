import http from './http'

// 各模块 API 统一封装，路径与 perf-server Controller 完全对齐
// 后端统一返回 { code, message, data }，http 拦截器已解包为该对象

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

export const slowLogApi = {
  // 慢查询日志内容解析入库
  parse: (data: { appName: string; dbHost?: string; content: string }) =>
    http.post('/slowlog/parse', data)
}

export const stressApi = {
  // 创建并启动压测任务（异步执行）
  create: (data: Record<string, unknown>) => http.post('/stress/tasks', data),
  // 任务列表
  list: (params?: { status?: string; limit?: number }) => http.get('/stress/tasks', { params }),
  // 任务详情
  detail: (id: number) => http.get(`/stress/tasks/${id}`),
  // 周期采样（实时图表）
  samples: (id: number, limit = 500) => http.get(`/stress/tasks/${id}/samples`, { params: { limit } }),
  // 压测报告（含统计/分档采样点/容量评估结果）
  report: (id: number) => http.get(`/stress/tasks/${id}/report`)
}

export const dataGenApi = {
  // 启动灌数据任务
  create: (data: Record<string, unknown>) => http.post('/datagen/tasks', data),
  // 任务列表
  list: (limit = 50) => http.get('/datagen/tasks', { params: { limit } }),
  // 任务详情（含进度）
  detail: (id: number) => http.get(`/datagen/tasks/${id}`),
  // 读取目标库表清单
  tables: (data: { dbUrl: string; dbUser: string; dbPassword: string }) =>
    http.post('/datagen/tables', data),
  // 读取目标表列结构
  columns: (data: { dbUrl: string; dbUser: string; dbPassword: string; tableName: string }) =>
    http.post('/datagen/columns', data)
}

export const capacityApi = {
  // 对压测任务执行容量评估（幂等，结果落库）
  analyze: (data: { taskId: number; targetP95Ms?: number; app?: string }) =>
    http.post('/capacity/analyze', data),
  // 已落库的容量评估结果
  stored: (taskId: number) => http.get(`/capacity/task/${taskId}`),
  // 实时扩容与 JVM 配置建议
  advice: (params?: { app?: string }) => http.get('/capacity/advice', { params })
}

export const sourceApi = {
  // 源码仓库列表
  repos: (limit = 50) => http.get('/source/repos', { params: { limit } }),
  // zip 上传导入（multipart）
  importZip: (repoName: string, file: File) => {
    const fd = new FormData()
    fd.append('repoName', repoName)
    fd.append('file', file)
    return http.post('/source/repos/zip', fd)
  },
  // git 地址导入
  importGit: (data: { repoName?: string; gitUrl: string }) => http.post('/source/repos/git', data),
  // 本地目录导入
  importLocal: (data: { repoName?: string; localPath: string }) => http.post('/source/repos/local', data),
  // 仓库文件树
  files: (repoId: number, params?: { prefix?: string; limit?: number }) =>
    http.get(`/source/repos/${repoId}/files`, { params }),
  // 文件内容
  fileContent: (fileId: number) => http.get(`/source/files/${fileId}/content`),
  // 性能问题列表
  issues: (params?: { issueType?: string; status?: string; limit?: number }) =>
    http.get('/source/issues', { params }),
  // 问题详情（含方案列表）
  issueDetail: (id: number) => http.get(`/source/issues/${id}`),
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
  // 从压测任务创建问题
  fromStress: (params: { stressTaskId: number; repoId: number }) =>
    http.post('/source/issues/from-stress', null, { params }),
  // 最近慢 SQL（源码页一键建问题用）
  recentSlowSqls: (params?: { app?: string; limit?: number }) =>
    http.get('/source/recent-slow-sqls', { params }),
  // 修复方案详情（含提示词）
  suggestion: (id: number) => http.get(`/source/suggestions/${id}`)
}

export const aiApi = {
  // AI 对话（Function Calling，服务端无 Key 时降级规则模式）
  chat: (data: { sessionId?: string; message: string }) => http.post('/ai/chat', data),
  // 工具清单
  tools: () => http.get('/ai/tools'),
  // 清空会话
  clearSession: (sessionId: string) => http.delete(`/ai/session/${sessionId}`)
}

export default { appApi, jvmApi, traceApi, slowSqlApi, slowLogApi, stressApi, dataGenApi, capacityApi, sourceApi, aiApi }
