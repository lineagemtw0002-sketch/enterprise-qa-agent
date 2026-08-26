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

/** 硬删除一个连接器。
 *
 * ⚠️ **级联清掉这个连接器下的全部数据**：权限授权、两张令牌表、修复动作、
 * 白名单配置、分析摘要。其中**修复动作和分析摘要是审计性质的**——
 * "谁在什么时候批准了什么、依据是什么"会跟着一起消失。
 * 所以 UI 上必须把这一点说清楚再让人点，不能只问一句"确定删除吗"。
 */
export function deleteConnector(connectionId) {
  return axios
    .delete(`${BASE}/ops/connectors/${encodeURIComponent(connectionId)}`)
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

// ==================== 运维权限授权（role_ops_systems） ====================
//
// 语义跟 role_collections 对齐：权限挂在**角色**上，不是挂在用户上——
// 一个用户一个角色，改角色的授权即刻对该角色全部成员生效，不用逐人配置
// （见 role_store.py 顶部说明）。
//
// ⚠️ `can_approve` 在后端会自动拉齐 `can_view`（能批准的人必然要能看见他在批什么）。
// 前端提交前也做同样的拉齐，理由不是"防止后端漏做"，而是**不让用户看到自己勾的和
// 保存后的不一致**——勾了 approve 没勾 view、保存回来变成两个都有，会被当成界面 bug。

export function listConnectorPermissions(connectionId) {
  return axios
    .get(`${BASE}/ops/connectors/${encodeURIComponent(connectionId)}/permissions`)
    .then((res) => res.data)
}

export function setRoleOpsPermission(roleId, connectionId, { can_view, can_approve }) {
  return axios
    .put(
      `${BASE}/roles/${encodeURIComponent(roleId)}/ops-permissions/${encodeURIComponent(connectionId)}`,
      { can_view: can_approve ? true : can_view, can_approve },
    )
    .then((res) => res.data)
}

export function revokeRoleOpsPermission(roleId, connectionId) {
  return axios
    .delete(`${BASE}/roles/${encodeURIComponent(roleId)}/ops-permissions/${encodeURIComponent(connectionId)}`)
    .then((res) => res.data)
}

// ==================== 分析摘要（告警关联时间线的数据源） ====================
//
// 只有真正调用过 analyze_ops_incident 才会有记录——没触发过分析的企业这里就是
// 空列表。**空列表要如实显示成空**，不要拿示例数据顶上（设计稿里那几条时间线是
// 示例数字，实现时按 mockup 头部注释的要求换成了这个真实接口）。
export function listAnalysisSummaries(limit = 20) {
  return axios
    .get(`${BASE}/ops/analysis-summaries`, { params: { limit } })
    .then((res) => res.data)
}

// ==================== V1 效果指标（§10.5） ====================
//
// ⚠️ 三个比例字段是 `Optional[float]`：**分母为 0 时后端返回 null，不是 0.0**。
// "还没有样本"和"比例恰好是 0"是两件不同的事，糊在一起会让"这家企业刚开始用、
// 数据太少"看起来像"表现很差"。前端必须把 null 显示成"暂无数据"。
export function getOpsMetrics() {
  return axios.get(`${BASE}/ops/metrics`).then((res) => res.data)
}

/** 事后人工标注"这次修复到底有没有解决问题"。
 *
 * 这是 §10.5 四个指标里唯一需要人工输入的一项——其余三个都能从状态机自己算出来。
 * 没有它，"执行成功"只代表命令跑通了，不代表问题解决了。
 */
export function setActionOutcome(actionId, effective) {
  return axios
    .post(`${BASE}/ops/remediation-actions/${encodeURIComponent(actionId)}/outcome`, { effective })
    .then((res) => res.data)
}
