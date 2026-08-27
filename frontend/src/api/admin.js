import axios from 'axios'

// 用相对路径复用 vite.config.js 里已经配好的 /api 代理；鉴权 token 走 axios 全局
// 拦截器（在 App.jsx 里挂的 axios.defaults.headers.common['Authorization']），
// 这里不用重复处理登录态。
const BASE = '/api/v1/admin'

// ==================== 用户管理 ====================

export function listUsers() {
  return axios.get(`${BASE}/users`).then((res) => res.data)
}

export function createUser({ username, password, role_ids, org_id }) {
  return axios.post(`${BASE}/users`, { username, password, role_ids, org_id }).then((res) => res.data)
}

export function deleteUser(userId) {
  return axios.delete(`${BASE}/users/${userId}`).then((res) => res.data)
}

export function setUserRoles(userId, roleIds) {
  return axios.put(`${BASE}/users/${userId}/roles`, { role_ids: roleIds }).then((res) => res.data)
}

// 停用 / 重新启用（docs/account_lifecycle_design.md §4.2）。
// 企业管理员能做的"离职处理"就是这个——删除自 2026-08-26 起只有平台管理员能做，
// 因为删除会带走 conversations 的归属，"离职员工做过什么"就再也追溯不到了。
export function setUserDisabled(userId, disabled) {
  return axios.put(`${BASE}/users/${userId}/disabled`, { disabled }).then((res) => res.data)
}

// CSV 批量导入（§4.1）。
//
// ⚠️ **validateOnly 默认 true，跟后端默认值保持一致。** 这是一个能一次影响上万
// 账号的操作，任何一层的默认值都必须是"什么都不做"。真跑必须由调用方显式传
// false —— 不要因为"前端反正会传"就把这里的默认值改掉，那样两边就都没有兜底了。
export function bulkImportUsers(file, validateOnly = true) {
  const form = new FormData()
  form.append('file', file)
  form.append('validate_only', String(validateOnly))
  return axios.post(`${BASE}/users/bulk-import`, form).then((res) => res.data)
}

// 席位上限（§4.4）。仅平台管理员，后端 require_platform_admin 兜底。
export function setSeatLimit(orgId, seatLimit) {
  return axios.put(`${BASE}/organizations/${orgId}/seat-limit`, { seat_limit: seatLimit }).then((res) => res.data)
}

// 账号激活。**唯一一个不在 /admin 下、也不需要登录态的接口** ——
// 调用它的人正是"还没有密码所以登不进来"的那个员工（§4.1b、风险 R-4）。
export function activateAccount({ username, activation_code, new_password }) {
  return axios
    .post('/api/v1/activate', { username, activation_code, new_password })
    .then((res) => res.data)
}

// ==================== 组织管理 ====================

export function listOrganizations() {
  return axios.get(`${BASE}/organizations`).then((res) => res.data)
}

export function createOrganization(name) {
  return axios.post(`${BASE}/organizations`, { name }).then((res) => res.data)
}

// ==================== 租户连接器（知识库/考勤委托，仅平台管理员） ====================

export function listTenantConnectors(orgId) {
  return axios.get(`${BASE}/organizations/${orgId}/connectors`).then((res) => res.data)
}

export function upsertTenantConnector(orgId, capability, payload) {
  return axios.put(`${BASE}/organizations/${orgId}/connectors/${capability}`, payload).then((res) => res.data)
}

// ==================== 网关监控（仅平台管理员） ====================

export function listGatewayConnectors() {
  return axios.get(`${BASE}/gateway/connectors`).then((res) => res.data)
}

// ==================== 角色管理 ====================
// 角色直接携带知识库权限（一人一角色）：平台管理员管全局角色（系统权限档位+
// 跨企业共用的部门身份，没有知识库配置入口）；企业管理员管自己企业的角色，
// 能建/改名/删/配置知识库关联，同一组接口，权限档位不同看到的范围不同——
// 见 role_store.py / app.py 角色管理 API 旁的说明。

export function listRoles() {
  return axios.get(`${BASE}/roles`).then((res) => res.data)
}

export function createRole({ name, display_name }) {
  return axios.post(`${BASE}/roles`, { name, display_name }).then((res) => res.data)
}

export function updateRole(roleId, displayName) {
  return axios.patch(`${BASE}/roles/${roleId}`, { display_name: displayName }).then((res) => res.data)
}

export function deleteRole(roleId) {
  return axios.delete(`${BASE}/roles/${roleId}`).then((res) => res.data)
}

export function setRoleCollections(roleId, collectionNames) {
  return axios.put(`${BASE}/roles/${roleId}/collections`, { collection_names: collectionNames }).then((res) => res.data)
}

// ==================== 知识库（仅企业管理员，只能看/建自己企业名下的） ====================

export function listCollections() {
  return axios.get(`${BASE}/collections`).then((res) => res.data)
}

export function createCollection({ collection_name, display_name }) {
  return axios.post(`${BASE}/collections`, { collection_name, display_name }).then((res) => res.data)
}

export function deleteCollection(collectionName) {
  return axios.delete(`${BASE}/collections/${collectionName}`).then((res) => res.data)
}

export function listCollectionChunks(collectionName, { offset = 0, limit = 20 } = {}) {
  return axios.get(`${BASE}/collections/${collectionName}/chunks`, { params: { offset, limit } }).then((res) => res.data)
}

// ==================== 运营仪表盘（仅平台管理员） ====================

export function getDashboardOverview(window) {
  return axios.get(`${BASE}/dashboard/overview`, { params: { window } }).then((res) => res.data)
}

export function getDashboardTrend(metric, window) {
  return axios.get(`${BASE}/dashboard/trend`, { params: { metric, window } }).then((res) => res.data)
}

// ==================== 成本与质量可观测性（仅平台管理员） ====================

export function getCostOverview(window) {
  return axios.get(`${BASE}/dashboard/cost-overview`, { params: { window } }).then((res) => res.data)
}

export function getCostTrend(metric, window) {
  return axios.get(`${BASE}/dashboard/cost-trend`, { params: { metric, window } }).then((res) => res.data)
}

// 2026-08-26 已删除：这里原有 adminTestQueryKnowledgeBase/adminTestListKbCollections/
// adminTestListKbChunks/adminTestClearKbCollection 四个函数，对应已删除的
// 【测试专用】知识库超权测试端点，见 `CLAUDE.md` §5「已修复」。企业知识库自助
// 管理走 deleteCollection/listCollectionChunks（见上方）。

// ==================== 审计日志（治理与合规） ====================
// 平台管理员能看全平台记录（可选 org_id 过滤某一家企业）；企业管理员只能看
// 自己企业的（后端强制，不管这里传不传 org_id 都会被忽略/覆盖）。

export function listAuditLogs(params) {
  return axios.get(`${BASE}/audit-logs`, { params }).then((res) => res.data)
}
