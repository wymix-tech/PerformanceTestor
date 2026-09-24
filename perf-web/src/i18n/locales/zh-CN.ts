/**
 * 中文文案（默认语言，P3-1）。
 *
 * 约定：
 * - key 按「区域.组件/页面.语义」分层，避免与页面文件一一绑定后重排困难；
 * - 只抽「用户可见文案」，日志/接口入参/枚举值不动；
 * - 参数统一用 `{name}` 具名占位，禁止拼接字符串（拼接无法被翻译按语序重排）。
 */
export default {
  common: {
    confirm: '确认',
    cancel: '取消',
    close: '关闭',
    save: '保存',
    delete: '删除',
    edit: '编辑',
    refresh: '刷新',
    loading: '加载中…',
    retry: '重试',
    search: '搜索',
    back: '返回',
    more: '加载更多',
    noData: '暂无数据',
    yes: '是',
    no: '否',
    copy: '复制',
    copied: '已复制',
    actions: '操作'
  },
  nav: {
    brandSub: 'Java 性能检测智能体',
    login: '登录',
    quickStart: '快速开始',
    notFound: '页面不存在',
    dashboard: '总览',
    projects: '项目管理',
    deploy: '部署管理',
    stress: '压测中心',
    jvm: '项目监控',
    trace: '调用链分析',
    optimize: '性能优化',
    ai: 'AI 助手',
    online: '平台在线',
    deployMode: '分离部署模式 · v1.0.0',
    currentTime: '当前时间 {time}',
    backToQuickStart: '返回快速开始',
    backToQuickStartTitle: '返回快速开始向导的当前步骤',
    notifications: '通知',
    settings: '设置',
    logout: '退出登录',
    logoutShort: '退出',
    defaultUser: '管理员'
  },
  theme: {
    toggle: '切换明暗主题',
    dark: '深色',
    light: '浅色',
    system: '跟随系统',
    current: '当前主题：{theme}'
  },
  language: {
    label: '语言',
    zhCN: '简体中文',
    enUS: 'English',
    switch: '切换语言'
  },
  a11y: {
    skipToContent: '跳到主内容',
    mainNav: '主导航',
    userMenu: '用户菜单',
    togglePassword: '显示或隐藏密码',
    sortAsc: '升序排列',
    sortDesc: '降序排列'
  },
  login: {
    title: 'Java 性能检测智能体平台',
    username: '账号',
    usernamePlaceholder: '请输入管理员账号',
    password: '密码',
    passwordPlaceholder: '请输入密码',
    submit: '登 录',
    submitting: '正在登录…',
    hint: '默认账号 admin / admin123，可通过环境变量 PERF_ADMIN_USER / PERF_ADMIN_PASSWORD 覆盖',
    footnote: '平台与被测应用分离部署 · 探针上报通道免鉴权',
    missingCredentials: '请输入账号与密码',
    failed: '登录失败',
    networkFailed: '登录失败，请检查网络连接'
  },
  notFound: {
    title: '页面不存在或已被移除',
    desc: '请检查地址是否正确，或返回总览继续性能检测之旅',
    backHome: '返回总览'
  }
} as const
