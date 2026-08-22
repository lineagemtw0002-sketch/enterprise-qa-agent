import { useEffect, useRef, useState } from 'react'
import { Modal, Select, Upload, Button, Alert, message, Tooltip, Tag, Progress } from 'antd'
import { UploadCloud, Lock } from 'lucide-react'
import * as collectionsApi from '../../api/collections.js'

// 上传到企业知识库——跟 App.jsx 里那个"知识库文件"抽屉（上传到私有的
// conv_{id} 临时对话 collection）是两回事：这里上传的是永久的、企业内共享的
// 知识库，所以要先让用户从"自己企业名下的全部知识库"里选一个目标（没权限的
// 选项置灰，见 accessible 字段），而不是像对话文件那样自动落到当前对话。
// 前端置灰只是 UX 提示，真正的权限校验在后端 upload_collection_document 里
// 会重新做一遍（见该端点旁的说明）——这里没权限选不中，不代表拼接口就能绕过。

// 委托模式企业上传时可选的子库/分类——跟 query_knowledge_hub.py
// DEPARTMENT_ROLE_TO_REMOTE_CATEGORIES / tenant_kb_demo/app.py CATEGORY_LABELS
// 用的是同一份 6 类目约定，这是平台给 demo 企业定的默认分类法，不是协议
// 强制要求，企业服务可以不认这个字段（见 collections.js uploadTenantKbDocument
// 旁的说明）。
const DELEGATED_CATEGORY_OPTIONS = [
  { value: 'hr_admin', label: '人力资源与行政' },
  { value: 'finance', label: '财务与报销制度' },
  { value: 'it_support', label: 'IT支持与技术运维' },
  { value: 'sales_marketing', label: '销售话术与市场' },
  { value: 'rd_product', label: '研发与产品代码' },
  { value: 'customer_success', label: '客户成功与售后服务' },
]

// 跟 pipeline.py on_progress 回调的阶段名一一对应，纯展示用的中文标签。
const STAGE_LABELS = {
  queued: '排队中',
  integrity: '校验文件',
  load: '解析文档内容',
  split: '智能切片',
  transform: '文本清洗与结构化优化',
  dedup: '内容去重检测',
  embed: '生成向量索引',
  upsert: '写入知识库',
}

const POLL_INTERVAL_MS = 700

export default function UploadToKbModal({ open, onClose }) {
  const [catalog, setCatalog] = useState([])
  const [catalogError, setCatalogError] = useState('')
  // 委托模式企业（getCollectionsCatalog 会报 400，见该端点旁 _require_local_
  // retrieval_org 的说明）没有 collection 概念可选，整个企业只有一份委托
  // 知识库——不是"暂不支持在这里上传"的死路，切到更简单的直传模式（方案 2，
  // 见 collections.js uploadTenantKbDocument 旁的说明）。
  const [delegatedMode, setDelegatedMode] = useState(false)
  const [delegatedCategory, setDelegatedCategory] = useState(undefined)
  const [loadingCatalog, setLoadingCatalog] = useState(false)
  const [selectedCollection, setSelectedCollection] = useState(null)
  const [pendingFile, setPendingFile] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [progress, setProgress] = useState(null)
  const pollTimerRef = useRef(null)

  function stopPolling() {
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current)
      pollTimerRef.current = null
    }
  }

  useEffect(() => {
    if (!open) {
      stopPolling()
      return
    }
    setSelectedCollection(null)
    setPendingFile(null)
    setProgress(null)
    setCatalogError('')
    setDelegatedMode(false)
    setDelegatedCategory(undefined)
    setLoadingCatalog(true)
    collectionsApi
      .getCollectionsCatalog()
      .then((data) => setCatalog(data))
      .catch((error) => {
        setCatalog([])
        if (error.response?.status === 400) {
          // 只有"这家企业是委托模式"这一种情况会从这个端点报 400（见
          // app.py `_require_local_retrieval_org`），其它错误码（401 未登录/
          // 403 未关联企业/5xx）仍然走下面的死路提示。
          setDelegatedMode(true)
        } else {
          setCatalogError(error.response?.data?.detail || error.message)
        }
      })
      .finally(() => setLoadingCatalog(false))

    return stopPolling
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  function pollProgress(uploadId) {
    collectionsApi
      .getUploadProgress(uploadId)
      .then((data) => {
        setProgress(data)
        if (data.done) {
          setSubmitting(false)
          if (data.success) {
            const dupHint = data.duplicate_chunk_count
              ? `，另有 ${data.duplicate_chunk_count} 个片段与库中已有内容重复已跳过`
              : ''
            message.success(`上传成功，新增 ${data.chunk_count} 个片段${dupHint}`)
            setPendingFile(null)
          } else {
            message.error('上传失败: ' + (data.error || '未知错误'))
          }
          return
        }
        pollTimerRef.current = setTimeout(() => pollProgress(uploadId), POLL_INTERVAL_MS)
      })
      .catch((error) => {
        setSubmitting(false)
        message.error('查询进度失败: ' + (error.response?.data?.detail || error.message))
      })
  }

  async function handleUpload() {
    if (!selectedCollection || !pendingFile) {
      message.warning('请先选择知识库和文件')
      return
    }
    setSubmitting(true)
    setProgress(null)
    try {
      const { upload_id } = await collectionsApi.startUpload(selectedCollection, pendingFile)
      setProgress({ stage: 'queued', current: 0, total: 1, done: false })
      pollProgress(upload_id)
    } catch (error) {
      setSubmitting(false)
      message.error('上传失败: ' + (error.response?.data?.detail || error.message))
    }
  }

  // 委托模式：平台切块+embedding 完成、推给企业自己的知识库微服务存好之后
  // 才返回（同步等待，没有 upload_id 轮询进度条），见 collections.js
  // uploadTenantKbDocument 旁的说明。
  async function handleDelegatedUpload() {
    if (!pendingFile) {
      message.warning('请先选择文件')
      return
    }
    setSubmitting(true)
    try {
      const data = await collectionsApi.uploadTenantKbDocument(pendingFile, delegatedCategory)
      if (data.chunk_count > 0) {
        message.success(`上传成功，新增 ${data.chunk_count} 个片段`)
        setPendingFile(null)
      } else {
        message.warning(data.message || '文档没有可摄入的内容')
      }
    } catch (error) {
      message.error('上传失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setSubmitting(false)
    }
  }

  const percent = progress && progress.total > 0 ? Math.round((progress.current / progress.total) * 100) : 0
  const progressStatus = progress?.done ? (progress.success ? 'success' : 'exception') : 'active'

  return (
    <Modal
      title="上传到企业知识库"
      open={open}
      onCancel={onClose}
      footer={null}
      destroyOnHidden
    >
      {catalogError ? (
        <Alert type="warning" showIcon message="暂不支持在这里上传" description={catalogError} />
      ) : delegatedMode ? (
        <>
          <p style={{ color: 'var(--text-tertiary, #888)', marginTop: 0 }}>
            你所在企业的知识库由企业自己的系统管理，这里上传的文档会由平台完成切片和向量化，
            再存入企业自己的知识库。可以选一个分类方便后续检索归类，不选也能上传。
          </p>

          <Select
            style={{ width: '100%', marginBottom: 16 }}
            placeholder="选择分类（可选）"
            allowClear
            value={delegatedCategory}
            onChange={setDelegatedCategory}
            disabled={submitting}
            options={DELEGATED_CATEGORY_OPTIONS}
          />

          <Upload.Dragger
            multiple={false}
            fileList={pendingFile ? [pendingFile] : []}
            beforeUpload={(file) => {
              setPendingFile(file)
              return false
            }}
            onRemove={() => setPendingFile(null)}
            disabled={submitting}
            accept=".pdf,.docx,.txt,.md,.csv,.xlsx,.xls,.pptx,.html,.htm,.json,.yaml,.yml"
          >
            <p className="ant-upload-drag-icon"><UploadCloud size={28} style={{ margin: '0 auto' }} /></p>
            <p>点击或拖拽文件到此处</p>
            <p style={{ color: 'var(--text-tertiary, #888)', fontSize: 12 }}>
              支持 PDF, Word, Excel, PPT, Markdown, HTML 等
            </p>
          </Upload.Dragger>

          <Button
            type="primary"
            style={{ marginTop: 16, width: '100%' }}
            loading={submitting}
            disabled={!pendingFile}
            onClick={handleDelegatedUpload}
          >
            上传并加入企业知识库
          </Button>
        </>
      ) : (
        <>
          <p style={{ color: 'var(--text-tertiary, #888)', marginTop: 0 }}>
            选择要加入的知识库，没有权限的知识库会显示为置灰状态——如需访问，请联系企业管理员在
            「权限系统」里给你的角色开通。
          </p>

          <Select
            style={{ width: '100%', marginBottom: 16 }}
            placeholder="选择知识库"
            loading={loadingCatalog}
            value={selectedCollection}
            onChange={setSelectedCollection}
            disabled={submitting}
            options={catalog.map((c) => ({
              value: c.collection_name,
              disabled: !c.accessible,
              label: c.accessible ? (
                c.display_name
              ) : (
                <Tooltip title="没有权限，无法选择">
                  <span>
                    <Lock size={12} style={{ marginRight: 4, verticalAlign: -1 }} />
                    {c.display_name}
                    <Tag style={{ marginLeft: 8 }}>无权限</Tag>
                  </span>
                </Tooltip>
              ),
            }))}
          />

          <Upload.Dragger
            multiple={false}
            fileList={pendingFile ? [pendingFile] : []}
            beforeUpload={(file) => {
              setPendingFile(file)
              return false
            }}
            onRemove={() => setPendingFile(null)}
            disabled={submitting}
            accept=".pdf,.docx,.txt,.md,.csv,.xlsx,.xls,.pptx,.html,.htm,.json,.yaml,.yml"
          >
            <p className="ant-upload-drag-icon"><UploadCloud size={28} style={{ margin: '0 auto' }} /></p>
            <p>点击或拖拽文件到此处</p>
            <p style={{ color: 'var(--text-tertiary, #888)', fontSize: 12 }}>
              支持 PDF, Word, Excel, PPT, Markdown, HTML 等
            </p>
          </Upload.Dragger>

          {progress && (
            <div style={{ marginTop: 16 }}>
              <Progress percent={percent} status={progressStatus} />
              <div style={{ color: 'var(--text-tertiary, #888)', fontSize: 12, marginTop: 4 }}>
                {progress.done
                  ? (progress.success ? '已完成' : '处理失败')
                  : `${STAGE_LABELS[progress.stage] || progress.stage}（${progress.current}/${progress.total}）`}
              </div>
            </div>
          )}

          {progress?.done && !progress.success && (
            <Alert style={{ marginTop: 16 }} type="error" showIcon message="上传失败" description={progress.error} />
          )}

          <Button
            type="primary"
            style={{ marginTop: 16, width: '100%' }}
            loading={submitting}
            disabled={!selectedCollection || !pendingFile}
            onClick={handleUpload}
          >
            上传并加入知识库
          </Button>
        </>
      )}
    </Modal>
  )
}
