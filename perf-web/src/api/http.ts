import axios from 'axios'
import router from '@/router'

// axios 实例：统一 baseURL 与错误处理
const http = axios.create({
  baseURL: '/api',
  timeout: 30000
})

// 请求拦截：自动注入 Bearer token（直接读 localStorage，避免与 store 循环依赖）
http.interceptors.request.use((config) => {
  const token = localStorage.getItem('perf_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

http.interceptors.response.use(
  (response) => response.data,
  (error) => {
    // HTTP 401：凭证缺失/过期，清除本地凭证并跳转登录页
    // 登录接口自身的 401（密码错误）不跳转，由登录页展示错误提示，防止死循环
    const isLoginRequest = !!error.config?.url?.includes('/auth/login')
    if (error.response?.status === 401 && !isLoginRequest) {
      localStorage.removeItem('perf_token')
      localStorage.removeItem('perf_username')
      if (router.currentRoute.value.name !== 'Login') {
        router.push({ name: 'Login', query: { redirect: router.currentRoute.value.fullPath } })
      }
    }
    console.error('API 请求失败:', error.message, error.response?.data)
    return Promise.reject(error)
  }
)

export default http
