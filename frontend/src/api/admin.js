import axios from 'axios'

// 用相对路径复用 vite.config.js 里已经配好的 /api 代理；鉴权 token 走 axios 全局
// 拦截器（在 App.jsx 里挂的 axios.defaults.headers.common['Authorization']），
// 这里不用重复处理登录态。
const BASE = '/api/v1/admin'

// ==================== 用户管理 ====================

export function listUsers() {
  return axios.get(`${BASE}/users`).then((res) => res.data)
}

export function createUser({ username, password, role_ids }) {
  return axios.post(`${BASE}/users`, { username, password, role_ids }).then((res) => res.data)
}

export function deleteUser(userId) {
  return axios.delete(`${BASE}/users/${userId}`).then((res) => res.data)
}

export function setUserRoles(userId, roleIds) {
  return axios.put(`${BASE}/users/${userId}/roles`, { role_ids: roleIds }).then((res) => res.data)
}

// ==================== 角色管理 ====================

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

// ==================== 知识库 ====================

export function listCollections() {
  return axios.get(`${BASE}/collections`).then((res) => res.data)
}
