import { resolve } from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 代理目标默认 8010（团队约定的后端端口）。
// 允许用 RAGENT_PROXY_TARGET 覆盖：本机经常同时有多个后端实例
// （不同会话各起一个），改代码去改端口再改回来容易漏掉、也会污染 diff。
const TARGET = process.env.RAGENT_PROXY_TARGET || 'http://localhost:8010'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  // 两个入口：主应用（浅色办公界面）和运维塔台（整屏深色监控大屏）。
  // 塔台单独开一页而不是做成主应用里的一个 tab —— 两种视觉语言塞进同一个壳里
  // 会互相打架（浅色顶栏 + 深色内容区），而且监控大屏本来就是独占一屏来盯的。
  // 用多页入口而不是引进前端路由：本项目没有 router，为一个页面引进它不划算，
  // 而多页入口在开发和构建下都是**真实的两个页面**，不依赖 SPA fallback。
  build: {
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'index.html'),
        ops: resolve(__dirname, 'ops.html'),
      },
    },
  },
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
