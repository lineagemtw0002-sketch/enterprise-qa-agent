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

// ==================== 事后复盘（§9.2 最小可行版） ====================
//
// 设计文档原话："没有这个视图，本模块的自动修复到底有没有用将无法被回顾评估，
// 是一条真实的遗留风险"。
//
// ⚠️ 返回项里的 `linked_summary` 是 `string | null`：**null 表示这条动作压根没有
// 关联 AI 分析（人工直接提议的），跟"关联了但摘要是空串"是两件事**。渲染时必须
// 分开表达——把 null 显示成"暂无"会让人以为分析跑过但没结论，而实际上是根本
// 没跑过分析就动手了，那是复盘时完全不同的一条线索。
export function listPostmortems(limit = 100) {
  return axios.get(`${BASE}/ops/postmortems`, { params: { limit } }).then((res) => res.data)
}

// ==================== 总览大屏：需要现场问连接器的那部分 ====================
//
// 服务健康网格 + 今日告警合并。跟 `getOpsMetrics()`（纯数据库统计）分开，
// 是因为这个要走联邦查询、耗时取决于客户环境，不该把纯数据库那几个指标一起拖慢。
//
// ⚠️ **`unavailable` 非空时必须在界面上显示出来。** 服务网格少了几个服务，
// 跟"这些服务都健康"在视觉上没有任何区别——不标注就是在骗人。
export function getLiveOverview() {
  return axios.get(`${BASE}/ops/live-overview`).then((res) => res.data)
}

// 把一条**已批准**的动作真正下发到客户环境。
//
// ⚠️ 在这个接口之前，模块唯一的执行通路是 LLM 工具——审批通过之后人在界面上
// 没有任何办法让它执行，只能去对话里跟模型说一句、指望它把参数传对（实测两次
// 都没传对）。后端这个端点走的是**跟 LLM 完全相同的那段执行代码**，四道检查
// 一模一样，不是一条更宽松的快捷路径。
//
// 409 = 被四道检查挡下（状态不对 / 白名单已被移除 / 并发冲突），是业务规则
// 冲突不是服务器错误，按提示刷新即可。
export function executeRemediationAction(actionId) {
  return axios
    .post(`${BASE}/ops/remediation-actions/${encodeURIComponent(actionId)}/execute`)
    .then((res) => res.data)
}

// ==================== 服务健康阈值 ====================
//
// 不同服务的可接受错误率差别很大——支付网关和内部报表不是一回事。一套固定阈值
// 判所有服务，要么把正常服务染红、要么把真故障判成正常，两者都会让人很快学会
// 忽略这个网格。
//
// `service = "*"` 表示该连接器的默认配置。解析顺序是**逐字段**回退：
// 具体服务的配置 → 连接器默认（`*`）→ 平台内置默认值。
//
// ⚠️ 返回项里 `thresholds` 是管理员实际填的那几个字段，`effective` 是叠加平台
// 默认之后真正生效的六个值——**两个都要用**：只显示前者，用户不知道没填的那几个
// 现在是多少；只显示后者，用户分不清哪些是自己配的。
export function listServiceThresholds(connectionId) {
  return axios
    .get(`${BASE}/ops/connectors/${encodeURIComponent(connectionId)}/service-thresholds`)
    .then((res) => res.data)
}

export function setServiceThresholds(connectionId, service, thresholds) {
  return axios
    .put(
      `${BASE}/ops/connectors/${encodeURIComponent(connectionId)}/service-thresholds/${encodeURIComponent(service)}`,
      { thresholds },
    )
    .then((res) => res.data)
}

export function deleteServiceThresholds(connectionId, service) {
  return axios
    .delete(`${BASE}/ops/connectors/${encodeURIComponent(connectionId)}/service-thresholds/${encodeURIComponent(service)}`)
    .then((res) => res.data)
}

// 对某个服务跑一次分析（异常检测 → 告警关联 → 根因辅助）。
//
// 在这个接口之前，`analyze_ops_incident` **只注册给了 LLM**——运维人员在塔台上
// 看到服务变红，没有任何办法让系统去分析它，只能去对话里说一句、指望 7B 模型
// 决定调那个工具（实测三次只成功一次）。跟"审批通过后没人能执行"是同一类缺陷。
//
// ⚠️ 返回的 `has_findings === false` **不是失败**，是"查了，没发现问题"。
// `degraded === true` 表示 RCA 那一步没有模型参与、结论只是数据复述，必须显式
// 告诉用户，否则他会把一段复述当成分析结论。
export function analyzeOpsIncident(target, { metric = 'error_rate', windowMinutes = 60 } = {}) {
  return axios
    .post(`${BASE}/ops/analyze`, { target, metric, window_minutes: windowMinutes })
    .then((res) => res.data)
}
