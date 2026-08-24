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

// ==================== 【测试专用，正式上线前删除】知识库超权测试查询 ====================
// 见 app.py admin_test_query_knowledge_base 端点旁的说明。

export function adminTestQueryKnowledgeBase({ org_id, query, top_k }) {
  return axios.post(`${BASE}/test/knowledge-query`, { org_id, query, top_k }).then((res) => res.data)
}

// 临时测试便利功能：查看/清空某企业知识库，方便反复测试"导入知识库->查询知识库"。
// 同样是【测试专用，正式上线前删除】的一部分。

export function adminTestListKbCollections(org_id) {
  return axios.get(`${BASE}/test/knowledge-query/collections`, { params: { org_id } }).then((res) => res.data)
}

export function adminTestListKbChunks({ org_id, collection, limit = 50 }) {
  return axios.get(`${BASE}/test/knowledge-query/chunks`, { params: { org_id, collection, limit } }).then((res) => res.data)
}

export function adminTestClearKbCollection({ org_id, collection }) {
  return axios.delete(`${BASE}/test/knowledge-query/collection`, { params: { org_id, collection } }).then((res) => res.data)
}

// ==================== 审计日志（治理与合规） ====================
// 平台管理员能看全平台记录（可选 org_id 过滤某一家企业）；企业管理员只能看
// 自己企业的（后端强制，不管这里传不传 org_id 都会被忽略/覆盖）。

export function listAuditLogs(params) {
  return axios.get(`${BASE}/audit-logs`, { params }).then((res) => res.data)
}
