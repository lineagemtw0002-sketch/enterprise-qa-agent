import axios from 'axios'

// 跟 admin.js 分开一个文件：那边的端点都在 /admin 前缀下、只对管理员开放；
// 这里是给任意登录员工用的知识库目录 + 上传自助入口（见 app.py
// collections_catalog / upload_collection_document 旁的说明）。
const BASE = '/api/v1/collections'

export function getCollectionsCatalog() {
  return axios.get(`${BASE}/catalog`).then((res) => res.data)
}

// 提交后立刻拿到 upload_id 就返回——真正的摄入在后端跑后台任务，不在这个
// 请求里等，配合 getUploadProgress 轮询显示进度条（见 app.py
// upload_collection_document 旁的说明）。
export function startUpload(collectionName, file) {
  const form = new FormData()
  form.append('file', file)
  return axios
    .post(`${BASE}/${encodeURIComponent(collectionName)}/documents`, form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    .then((res) => res.data)
}

export function getUploadProgress(uploadId) {
  return axios.get(`${BASE}/uploads/${encodeURIComponent(uploadId)}`).then((res) => res.data)
}

// 委托模式企业专用上传（方案 2："平台代算，企业只存储"，见 app.py
// upload_tenant_kb_document / knowledge-base-tenant-federation.md 第 4.4 节）——
// 没有本地 collection 概念可选，整个企业只有一份委托知识库；`category` 是
// 可选的子库/分类标签（企业服务自己决定怎么用，见该端点旁的说明），不是
// 强制字段。同步等待整条链路完成才返回，不是本地模式那套 upload_id 轮询
// 进度条。
export function uploadTenantKbDocument(file, category) {
  const form = new FormData()
  form.append('file', file)
  if (category) form.append('category', category)
  return axios
    .post('/api/v1/tenant-kb/documents', form, { headers: { 'Content-Type': 'multipart/form-data' } })
    .then((res) => res.data)
}
