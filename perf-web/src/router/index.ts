import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/LoginView.vue'),
    meta: { title: '登录', public: true }
  },
  {
    path: '/',
    component: () => import('@/layouts/MainLayout.vue'),
    redirect: '/dashboard',
    children: [
      {
        path: 'dashboard',
        name: 'Dashboard',
        component: () => import('@/views/DashboardView.vue'),
        meta: { title: '总览' }
      },
      {
        path: 'quick-start',
        name: 'QuickStart',
        component: () => import('@/views/QuickStartView.vue'),
        meta: { title: '快速开始' }
      },
      {
        path: 'projects',
        name: 'ProjectManage',
        component: () => import('@/views/ProjectManageView.vue'),
        meta: { title: '项目管理' }
      },
      {
        path: 'dbconns',
        name: 'DbConnManage',
        component: () => import('@/views/DbConnManageView.vue'),
        meta: { title: '数据库连接' }
      },
      {
        path: 'deploy',
        name: 'DeployManage',
        component: () => import('@/views/DeployManageView.vue'),
        meta: { title: '部署管理' }
      },
      {
        path: 'jvm',
        name: 'JvmMonitor',
        component: () => import('@/views/JvmMonitorView.vue'),
        meta: { title: '项目监控' }
      },
      {
        path: 'trace',
        name: 'TraceAnalyzer',
        component: () => import('@/views/TraceAnalyzerView.vue'),
        meta: { title: '调用链分析' }
      },
      {
        path: 'stress',
        name: 'StressCenter',
        component: () => import('@/views/StressCenterView.vue'),
        meta: { title: '压测中心' }
      },
      {
        path: 'datasets',
        name: 'DatasetManage',
        component: () => import('@/views/DatasetManageView.vue'),
        meta: { title: '数据管理' }
      },
      {
        path: 'optimize',
        name: 'Optimize',
        component: () => import('@/views/OptimizeView.vue'),
        meta: { title: '性能优化' }
      },
      {
        path: 'ai',
        name: 'AiAssistant',
        component: () => import('@/views/AiAssistantView.vue'),
        meta: { title: 'AI 助手' }
      }
    ]
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'NotFound',
    component: () => import('@/views/NotFoundView.vue'),
    meta: { title: '页面不存在', public: true }
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

// 路由守卫：未登录跳转登录页（携带回跳地址），已登录访问登录页跳总览
// 直接读 localStorage 避免依赖 Pinia 实例（守卫执行时 store 可能未就绪）
router.beforeEach((to) => {
  const token = localStorage.getItem('perf_token')
  if (!to.meta.public && !token) {
    return { name: 'Login', query: { redirect: to.fullPath } }
  }
  if (to.name === 'Login' && token) {
    return { name: 'Dashboard' }
  }
  return true
})

// 路由标题同步：meta.title → document.title（浏览器标签页/书签可辨识）
router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} · PerformanceTestor` : 'PerformanceTestor'
})

export default router
