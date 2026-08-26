import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 代理目标默认 8010（团队约定的后端端口）。
// 允许用 RAGENT_PROXY_TARGET 覆盖：本机经常同时有多个后端实例
// （不同会话各起一个），改代码去改端口再改回来容易漏掉、也会污染 diff。
const TARGET = process.env.RAGENT_PROXY_TARGET || 'http://localhost:8010'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5190,
    strictPort: true,
    proxy: {
      '/api': {
        target: TARGET,
        changeOrigin: true,
      },
      '/ws': {
        target: TARGET.replace(/^http/, 'ws'),
        ws: true,
        changeOrigin: true,
      }
    }
  }
})
