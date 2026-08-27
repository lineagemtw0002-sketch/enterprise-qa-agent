/**
 * 运维塔台的独立页面外壳（`/ops.html` 的根组件）。
 *
 * 相对主应用里那个 tab 版本，这里多做三件事：
 * 1. **自己引导鉴权**——它不在 App.jsx 的组件树里，拿不到那边挂的 axios 拦截器，
 *    所以要自己从 localStorage 取 token 挂上（跟 App.jsx 顶部同一个做法：
 *    **在任何子组件挂载之前**同步挂好，不能等 useEffect，否则一挂载就发请求的
 *    子组件会先吃一个 401）。
 * 2. **顶栏按设计稿还原**——品牌标 + 「运维塔台」+ 企业名 + 实时接入 + 时钟。
 *    tab 版本没有顶栏（主应用的顶栏顶着），独立页面必须自己有。
 * 3. **把整页染深**——`.ops-page` 撑满视口。深色只存在于这一页，主应用一点没变，
 *    这正是把它拆出来的原因。
 */
import { useEffect, useMemo, useState } from 'react'
import axios from 'axios'
import { ConfigProvider, Spin, theme } from 'antd'
import OpsConsole from './OpsConsole.jsx'
import './OpsConsole.css'
import './OpsStandalone.css'

// ⚠️ 同步挂 token，不放进 useEffect。理由见文件头第 1 条。
const _token = localStorage.getItem('ragent_token')
if (_token) {
  axios.defaults.headers.common.Authorization = `Bearer ${_token}`
}

// 跟 App.jsx 第 23 行同一份判据。**刻意重复一份常量而不是从 App.jsx 导入**：
// 那个文件是主应用的根组件，导入它会把整个主应用（包括对话、知识库那些）
// 一起打进塔台这个入口的 bundle 里。两个字符串的重复，换掉一整棵依赖树。
const ADMIN_ROLE_NAMES = new Set(['super_admin', 'org_admin'])

export const OPS_THEME = {
  algorithm: theme.darkAlgorithm,
  token: {
    colorBgBase: '#12131a',
    colorPrimary: '#8b7ffb',
    colorSuccess: '#34d399',
    colorWarning: '#fbbf24',
    colorError: '#f8717a',
    borderRadius: 8,
  },
}

/** 顶栏右侧的时钟。设计稿里它是走字的——一块监控大屏上不走的时钟会让人
 *  怀疑整个页面是不是卡住了，所以这里真的每秒更新，不是摆一个静态时间。 */
function Clock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(t)
  }, [])
  const text = useMemo(() => {
    const p = (n) => String(n).padStart(2, '0')
    return `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}  `
      + `${p(now.getHours())}:${p(now.getMinutes())}:${p(now.getSeconds())}`
  }, [now])
  return <div className="ops-clock">{text}</div>
}

export default function OpsStandalone() {
  const [me, setMe] = useState(null)
  const [connectors, setConnectors] = useState(null)
  const [state, setState] = useState(_token ? 'loading' : 'anonymous')

  useEffect(() => {
    if (!_token) return
    axios.get('/api/v1/auth/me')
      .then((res) => { setMe(res.data); setState('ready') })
      .catch(() => setState('anonymous'))
  }, [])

  if (state === 'anonymous') {
    // 独立页面可能被直接收藏/粘贴打开，这时没有登录态。
    // **不自作主张跳转到登录页**——那会把用户已经打开的主应用标签页之外
    // 又扔进一个登录流程，而他大概率只是在另一个标签页里已经登录了。
    // 给一句话说清楚该怎么做就够了。
    return (
      <ConfigProvider theme={OPS_THEME}>
        <div className="ops-page ops-page-center">
          <div className="ops-empty-hint">
            <h1>运维塔台</h1>
            <p>请先在主应用里登录，然后从顶部导航的「智能运维」进来。</p>
            <a href="/">前往主应用</a>
          </div>
        </div>
      </ConfigProvider>
    )
  }

  if (state === 'loading') {
    return (
      <ConfigProvider theme={OPS_THEME}>
        <div className="ops-page ops-page-center"><Spin size="large" /></div>
      </ConfigProvider>
    )
  }

  // 跟主应用 TopNav 同一套判据：org_admin 能管配置，被授予 can_view 的普通
  // 员工只看总览和审批队列。**这里不能只看角色名**——`role_ops_systems` 落地后
  // 审批人可以是任意被授权的角色。
  // ⚠️ `/auth/me` 的 `roles` 是**对象数组**（`[{name, ...}]`），不是字符串数组。
  // 第一版写成 `r === 'org_admin'` 恒为 false —— 页面照常渲染，只是所有管理
  // 分段静默消失，看起来像"权限不够"而不是 bug。这类猜字段名的错在这条产品线
  // 上已经出现过两次，跟主应用用同一个判据抄过来，不要凭印象写。
  const canManage = me?.roles?.some((r) => ADMIN_ROLE_NAMES.has(r.name)) ?? false
  const orgName = me?.organization?.name || '—'
  const online = (connectors || []).filter((c) => c.connector_status === 'online').length

  return (
    <ConfigProvider theme={OPS_THEME}>
      <div className="ops-page">
        <header className="ops-topbar">
          <div className="ops-brand">
            <div className="ops-brand-mark" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none">
                <path d="M12 3L4 7v6c0 4.6 3.2 8.4 8 9 4.8-.6 8-4.4 8-9V7l-8-4z"
                      stroke="#0d0b1f" strokeWidth="1.6" strokeLinejoin="round" />
                <path d="M9 12.5l2 2 4-4.5"
                      stroke="#0d0b1f" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </div>
            <div className="ops-brand-text">
              <h1>运维塔台</h1>
              <div className="sub">AIOPS · OPERATIONS TOWER</div>
            </div>
          </div>
          <div className="ops-topbar-right">
            <div className="ops-org"><span className="ops-org-dot" />{orgName}</div>
            {/* 设计稿里的「实时接入」指示。数据来自塔台已经拉过的连接器列表，
                不额外发请求。**没拿到列表时整块不渲染**，不显示"0 个在线"
                ——"还没查到"和"一个都没在线"在监控界面上是两回事。 */}
            {connectors && (
              <div className={`ops-live-pill ${online > 0 ? 'live' : 'idle'}`}>
                <span className="ops-live-dot" />
                {online > 0 ? `实时接入 · ${online} 个连接器在线` : '无连接器在线'}
              </div>
            )}
            <a className="ops-backlink" href="/">← 返回主应用</a>
            <Clock />
          </div>
        </header>

        <OpsConsole canManage={canManage} onConnectorsChange={setConnectors} />
      </div>
    </ConfigProvider>
  )
}
