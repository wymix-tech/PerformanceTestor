import axios from 'axios'

// axios 实例：统一 baseURL 与错误处理
const http = axios.create({
  baseURL: '/api',
  timeout: 30000
})

http.interceptors.response.use(
  (response) => response.data,
  (error) => {
    console.error('API 请求失败:', error.message, error.response?.data)
    return Promise.reject(error)
  }
)

export default http
