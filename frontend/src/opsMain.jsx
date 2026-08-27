/**
 * 运维塔台的**独立页面入口**（`/ops.html`）。
 *
 * 为什么单独开一个页面，而不是主应用里的一个 tab：
 * 塔台是整屏深色的监控大屏，主应用是浅色的办公界面。塞在同一个壳里，
 * 顶栏、侧边栏、弹窗全是浅色，中间一块深色，两种视觉语言互相打架。
 * 监控大屏本来就是"盯着看"的场景，独占一个标签页也更符合它的用法。
 *
 * 为什么用 vite 多页入口而不是前端路由：
 * 这个项目没有 react-router，整站靠 state 切 view。为了一个页面引进路由
 * 是杀鸡用牛刀；而多页入口在**开发和构建下都是真实的两个页面**，
 * 不依赖任何 SPA fallback 配置，也不会有白闪。
 */
import React from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider } from 'antd'
import OpsStandalone, { OPS_THEME } from './components/ops/OpsStandalone.jsx'
import './index.css'

// ⚠️ **antd 的静态 `message.success(...)` 拿不到 ConfigProvider 的主题。**
// 它挂在组件树之外的一个独立 holder 上，所以在这块整屏深色的大屏上，
// 提示条会是**白色的**弹出来——很刺眼，而且看起来像是别的页面的东西。
// antd 官方给的解法是改用 `App.useApp()`，但那要改掉塔台里十几处调用点；
// `holderRender` 是同一个问题的全局解，一处生效，且不动任何调用点。
ConfigProvider.config({
  holderRender: (children) => <ConfigProvider theme={OPS_THEME}>{children}</ConfigProvider>,
})

ReactDOM.createRoot(document.getElementById('ops-root')).render(
  <React.StrictMode>
    <OpsStandalone />
  </React.StrictMode>
)
