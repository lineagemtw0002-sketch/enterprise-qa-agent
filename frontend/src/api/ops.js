import axios from 'axios'

// 智能运维模块（运维塔台）的接口封装。设计见 docs/aiops_module_design.md。
// 跟 admin.js 一样用相对路径复用 vite 代理，鉴权走 App.jsx 挂的 axios 全局拦截器。
//
// ⚠️ 这些端点在企业未开通智能运维模块时统一返回 403（后端 _require_aiops_enabled_org
// 是叠加在 ACL 之前的独立一层）。调用方应该用下面的 isModuleDisabledError 把这种
// "还没开通"跟真正的"没权限"区分开——两者对用户来说是完全不同的事：前者要去找
// 平台管理员开通，后者是找错了人。
const BASE = '/api/v1/admin'

/** 后端把"模块未开通"和"越权"都表达成 403，这里按提示文案区分。
 *
 * ⚠️ 这是**基于文案的启发式判断**，不是可靠契约——后端改了提示词这里就会失灵。
 * 更稳的做法是后端给一个机器可读的 error code，已经反馈给后端那条线。
 * 在那之前，判错的后果只是提示语不够贴切（仍然会显示一个 403 的错误态），
 * 不会造成功能性问题，所以先这么用。
 */
export function isModuleDisabledError(error) {
  if (error?.response?.status !== 403) return false
  const detail = String(error.response?.data?.detail || '')
  return detail.includes('智能运维') || detail.includes('aiops') || detail.includes('未开通')
}

/** 统一把 axios 错误转成能直接显示给用户的一句话。 */
export function errorText(error) {
  return error?.response?.data?.detail || error?.message || '请求失败'
}

// ==================== 连接器 ====================

export function listConnectors() {
  return axios.get(`${BASE}/ops/connectors`).then((res) => res.data)
}

export function registerConnector({ name, system_type, approval_timeout_minutes }) {
  return axios
    .post(`${BASE}/ops/connectors`, { name, system_type, approval_timeout_minutes })
    .then((res) => res.data)
}

/** 生成一次性握手凭证。
 *
 * ⚠️ **返回的 register_token 是明文，而且只在这一次响应里出现**（平台只存哈希，
 * 见 OpsConnectorRegisterTokenResponse 的字段说明）。调用方必须把它当作
 * "关掉就再也拿不到"的东西呈现——不能塞进列表里当普通字段渲染，也不要写进
 * 任何日志。这不是谨慎过头：平台自己都查不出来它是什么。
 */
export function generateRegisterToken(connectionId) {
  return axios
    .post(`${BASE}/ops/connectors/${encodeURIComponent(connectionId)}/register-token`)
    .then((res) => res.data)
}

// ==================== 修复范围白名单 ====================

export function listRemediationScopes(connectionId) {
  return axios
    .get(`${BASE}/ops/connectors/${encodeURIComponent(connectionId)}/remediation-scopes`)
    .then((res) => res.data)
}

export function upsertRemediationScope(connectionId, actionType, scopeConfig) {
  return axios
    .put(
      `${BASE}/ops/connectors/${encodeURIComponent(connectionId)}/remediation-scopes/${encodeURIComponent(actionType)}`,
      { scope_config: scopeConfig },
    )
    .then((res) => res.data)
}

// ==================== 修复动作 ====================

export function listRemediationActions() {
  return axios.get(`${BASE}/ops/remediation-actions`).then((res) => res.data)
}

export function proposeRemediationAction(connectionId, { action_type, intent, plan, impact_radius }) {
  return axios
    .post(`${BASE}/ops/connectors/${encodeURIComponent(connectionId)}/remediation-actions`, {
      action_type,
      intent,
      plan,
      impact_radius,
    })
    .then((res) => res.data)
}

export function approveRemediationAction(actionId) {
  return axios
    .post(`${BASE}/ops/remediation-actions/${encodeURIComponent(actionId)}/approve`)
    .then((res) => res.data)
}

export function rejectRemediationAction(actionId) {
  return axios
    .post(`${BASE}/ops/remediation-actions/${encodeURIComponent(actionId)}/reject`)
    .then((res) => res.data)
}

// ==================== 模块开关（仅平台管理员） ====================

export function setAiopsModuleEnabled(orgId, enabled) {
  return axios
    .put(`${BASE}/organizations/${encodeURIComponent(orgId)}/aiops-module-enabled`, { enabled })
    .then((res) => res.data)
}
