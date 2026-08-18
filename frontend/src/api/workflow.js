import axios from 'axios'

// 复用 App.jsx 挂的全局 token 拦截器，这里不重复处理鉴权（对齐 api/admin.js 的写法）
const BASE = '/api/v1'

// ==================== 流程模板 ====================

export function listWorkflowTemplates() {
  return axios.get(`${BASE}/workflow-templates`).then((res) => res.data)
}

export function listApprovableWorkflowTypes() {
  return axios.get(`${BASE}/workflow-templates/approvable-types`).then((res) => res.data)
}

// ==================== 管理后台：流程模板 CRUD（仅 super_admin） ====================

export function adminListWorkflowTemplates() {
  return axios.get(`${BASE}/admin/workflow-templates`).then((res) => res.data)
}

export function adminCreateWorkflowTemplate(payload) {
  return axios.post(`${BASE}/admin/workflow-templates`, payload).then((res) => res.data)
}

export function adminUpdateWorkflowTemplate(templateId, payload) {
  return axios.patch(`${BASE}/admin/workflow-templates/${templateId}`, payload).then((res) => res.data)
}

export function adminDeleteWorkflowTemplate(templateId) {
  return axios.delete(`${BASE}/admin/workflow-templates/${templateId}`).then((res) => res.data)
}

// ==================== 工作流实例 ====================

export function listMyWorkflows(status) {
  return axios.get(`${BASE}/workflows`, { params: status ? { status } : {} }).then((res) => res.data)
}

export function listPendingApprovalWorkflows() {
  return axios.get(`${BASE}/workflows/pending-approval`).then((res) => res.data)
}

export function getWorkflow(instanceId) {
  return axios.get(`${BASE}/workflows/${instanceId}`).then((res) => res.data)
}

export function approveWorkflow(instanceId, comment) {
  return axios.post(`${BASE}/workflows/${instanceId}/approve`, { comment: comment || null }).then((res) => res.data)
}

export function returnWorkflow(instanceId, comment) {
  return axios.post(`${BASE}/workflows/${instanceId}/return`, { comment }).then((res) => res.data)
}

export function rejectWorkflow(instanceId, comment) {
  return axios.post(`${BASE}/workflows/${instanceId}/reject`, { comment }).then((res) => res.data)
}

export function resubmitWorkflow(instanceId) {
  return axios.post(`${BASE}/workflows/${instanceId}/resubmit`).then((res) => res.data)
}

export function completeWorkflow(instanceId) {
  return axios.post(`${BASE}/workflows/${instanceId}/complete`).then((res) => res.data)
}

export function cancelWorkflow(instanceId) {
  return axios.post(`${BASE}/workflows/${instanceId}/cancel`).then((res) => res.data)
}

// ==================== 文件（工作流详情里展示申请人传的材料用） ====================

export function listConversationFiles(conversationId) {
  return axios.get(`${BASE}/conversations/${conversationId}/files`).then((res) => res.data.files || [])
}

export async function downloadFile(conversationId, fileId, filename) {
  // 下载端点需要 Authorization header，普通 <a href> 拿不到 token，走 axios 取 blob
  // 再触发一次浏览器原生下载（标准做法，不是 App.jsx 现有代码里已有的模式，这里
  // 是第一次需要下载文件而不是预览/上传）。
  const res = await axios.get(`${BASE}/conversations/${conversationId}/files/${fileId}/download`, {
    responseType: 'blob',
  })
  const url = window.URL.createObjectURL(res.data)
  const link = document.createElement('a')
  link.href = url
  link.download = filename || 'file'
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.URL.revokeObjectURL(url)
}
