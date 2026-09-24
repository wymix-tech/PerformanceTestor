/**
 * English messages (P3-1). 结构与 zh-CN 完全一致，缺失 key 会在测试中断言。
 * 仅覆盖批次 4 抽取的骨架文案（导航/登录/通用/主题/语言/无障碍），
 * 业务页文案仍按需分批迁移——不做「一次性全站替换」以免引入大量错译。
 */
export default {
  common: {
    confirm: 'Confirm',
    cancel: 'Cancel',
    close: 'Close',
    save: 'Save',
    delete: 'Delete',
    edit: 'Edit',
    refresh: 'Refresh',
    loading: 'Loading…',
    retry: 'Retry',
    search: 'Search',
    back: 'Back',
    more: 'Load more',
    noData: 'No data',
    yes: 'Yes',
    no: 'No',
    copy: 'Copy',
    copied: 'Copied',
    actions: 'Actions'
  },
  nav: {
    brandSub: 'Java Performance Agent',
    login: 'Sign in',
    quickStart: 'Quick Start',
    notFound: 'Not found',
    dashboard: 'Overview',
    projects: 'Projects',
    deploy: 'Deployment',
    stress: 'Load Testing',
    jvm: 'Monitoring',
    trace: 'Trace Analysis',
    optimize: 'Optimization',
    ai: 'AI Assistant',
    online: 'Platform online',
    deployMode: 'Split deployment · v1.0.0',
    currentTime: 'Current time {time}',
    backToQuickStart: 'Back to Quick Start',
    backToQuickStartTitle: 'Return to the current Quick Start step',
    notifications: 'Notifications',
    settings: 'Settings',
    logout: 'Sign out',
    logoutShort: 'Sign out',
    defaultUser: 'Admin'
  },
  theme: {
    toggle: 'Toggle light/dark theme',
    dark: 'Dark',
    light: 'Light',
    system: 'Follow system',
    current: 'Current theme: {theme}'
  },
  language: {
    label: 'Language',
    zhCN: '简体中文',
    enUS: 'English',
    switch: 'Switch language'
  },
  a11y: {
    skipToContent: 'Skip to main content',
    mainNav: 'Main navigation',
    userMenu: 'User menu',
    togglePassword: 'Show or hide password',
    sortAsc: 'Sort ascending',
    sortDesc: 'Sort descending'
  },
  login: {
    title: 'Java Performance Agent Platform',
    username: 'Username',
    usernamePlaceholder: 'Enter admin username',
    password: 'Password',
    passwordPlaceholder: 'Enter password',
    submit: 'Sign in',
    submitting: 'Signing in…',
    hint: 'Default account admin / admin123; override via PERF_ADMIN_USER / PERF_ADMIN_PASSWORD',
    footnote: 'Platform and target app deploy separately · Agent reporting is unauthenticated',
    missingCredentials: 'Please enter username and password',
    failed: 'Sign-in failed',
    networkFailed: 'Sign-in failed, please check your network connection'
  },
  notFound: {
    title: 'Page not found or removed',
    desc: 'Check the URL, or go back to the overview to continue',
    backHome: 'Back to overview'
  }
} as const
