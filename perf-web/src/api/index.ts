import http from './http'

// 各模块 API 统一封装：JVM 指标 / 调用链 / 慢 SQL / 压测 / 灌数据 / 源码 / 容量 / AI

export const jvmApi = {
  // 查询 JVM 指标时序数据
  metrics: (params: { appName?: string; rangeMin?: number }) =>
    http.get('/jvm/metrics', { params }),
  // 应用列表
  apps: () => http.get('/jvm/apps')
}

export const traceApi = {
  // 查询调用链列表
  list: (params: { appName?: string; startTime?: string; limit?: number }) =>
    http.get('/trace/list', { params }),
  // 调用链详情（含调用树 JSON）
  detail: (id: number) => http.get(`/trace/detail/${id}`)
}

export const slowSqlApi = {
  // 查询慢 SQL 列表
  list: (params: { appName?: string; source?: string; limit?: number }) =>
    http.get('/slow-sql/list', { params }),
  // 触发慢日志解析任务
  parseLog: (params: { logPath: string; dbHost?: string }) =>
    http.post('/slow-sql/parse', params)
}

export const stressApi = {
  // 创建并启动压测任务
  start: (data: Record<string, unknown>) => http.post('/stress/start', data),
  // 任务列表
  list: () => http.get('/stress/list'),
  // 任务详情（含结果 JSON / 容量评估）
  detail: (id: number) => http.get(`/stress/detail/${id}`),
  // 任务实时采样
  samples: (id: number) => http.get(`/stress/samples/${id}`)
}

export const dataGenApi = {
  // 启动灌数据任务
  start: (data: Record<string, unknown>) => http.post('/datagen/start', data),
  // 任务列表
  list: () => http.get('/datagen/list'),
  // 读取目标库表结构
  tables: (params: { dbUrl: string; username: string; password: string }) =>
    http.post('/datagen/tables', params)
}

export const sourceApi = {
  // 源码仓库列表
  repos: () => http.get('/source/repos'),
  // 导入源码（zip/git/local）
  import: (data: Record<string, unknown>) => http.post('/source/import', data),
  // 仓库文件树
  files: (repoId: number) => http.get(`/source/files/${repoId}`),
  // 文件内容
  fileContent: (fileId: number) => http.get(`/source/file/${fileId}`),
  // 性能问题列表
  issues: (params?: Record<string, unknown>) => http.get('/source/issues', { params }),
  // 问题详情（含修复方案）
  issueDetail: (id: number) => http.get(`/source/issues/${id}`),
  // 选定修复方案并生成修复提示词
  selectSuggestion: (suggestionId: number) =>
    http.post(`/source/suggestions/${suggestionId}/select`),
  // 触发问题分析（大模型）
  analyze: (issueId: number) => http.post(`/source/issues/${issueId}/analyze`)
}

export const capacityApi = {
  // 容量评估与扩容建议（基于压测任务）
  advice: (taskId: number) => http.get(`/capacity/advice/${taskId}`)
}

export const aiApi = {
  // AI 对话（Function Calling）
  chat: (messages: Array<Record<string, unknown>>) => http.post('/ai/chat', { messages })
}

export default { jvmApi, traceApi, slowSqlApi, stressApi, dataGenApi, sourceApi, capacityApi, aiApi }
