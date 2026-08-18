import { useEffect, useState } from 'react'
import { Drawer, Tag, Timeline, Modal, Input, Button, Space, message, Empty, Spin } from 'antd'
import { Download, FileText } from 'lucide-react'
import * as workflowApi from '../../api/workflow.js'
import { workflowStatusMeta } from './workflowMeta.js'

const EVENT_LABEL = {
  submitted: '提交申请',
  approved: '审批通过',
  returned: '打回补充材料',
  rejected: '驳回',
  resubmitted: '重新提交',
  completed: '标记完成',
  cancelled: '取消申请',
}

function formatTs(ts) {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleString('zh-CN')
}

// 两个 Tab（我发起的 / 待我审批）共用同一个详情组件，用 meUserId 跟 instance
// 的 requester_user_id 比较来判断这次是以哪个身份在看，决定显示哪些操作按钮
// （work-flow-web.md 4.3 节）。
export default function WorkflowDetailDrawer({ instanceId, open, onClose, meUserId, onChanged }) {
  const [instance, setInstance] = useState(null)
  const [template, setTemplate] = useState(null)
  const [files, setFiles] = useState([])
  const [loading, setLoading] = useState(false)
  const [actionLoading, setActionLoading] = useState(false)
  const [returnModalOpen, setReturnModalOpen] = useState(false)
  const [rejectModalOpen, setRejectModalOpen] = useState(false)
  const [comment, setComment] = useState('')

  useEffect(() => {
    if (!open || !instanceId) return
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, instanceId])

  async function load() {
    setLoading(true)
    try {
      const inst = await workflowApi.getWorkflow(instanceId)
      setInstance(inst)
      const templates = await workflowApi.listWorkflowTemplates()
      setTemplate(templates.find((t) => t.workflow_type === inst.workflow_type) || null)
      if (inst.conversation_id) {
        const fs = await workflowApi.listConversationFiles(inst.conversation_id)
        setFiles(fs)
      } else {
        setFiles([])
      }
    } catch (error) {
      message.error('加载详情失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setLoading(false)
    }
  }

  function notifyChangedAndClose() {
    onChanged?.()
    onClose()
  }

  async function handleApprove() {
    setActionLoading(true)
    try {
      await workflowApi.approveWorkflow(instanceId)
      message.success('已通过')
      notifyChangedAndClose()
    } catch (error) {
      message.error('操作失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setActionLoading(false)
    }
  }

  async function submitReturn() {
    if (!comment.trim()) {
      message.warning('请写清楚缺什么材料')
      return
    }
    setActionLoading(true)
    try {
      await workflowApi.returnWorkflow(instanceId, comment.trim())
      message.success('已打回')
      setReturnModalOpen(false)
      notifyChangedAndClose()
    } catch (error) {
      message.error('操作失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setActionLoading(false)
    }
  }

  async function submitReject() {
    if (!comment.trim()) {
      message.warning('请填写驳回原因')
      return
    }
    setActionLoading(true)
    try {
      await workflowApi.rejectWorkflow(instanceId, comment.trim())
      message.success('已驳回')
      setRejectModalOpen(false)
      notifyChangedAndClose()
    } catch (error) {
      message.error('操作失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setActionLoading(false)
    }
  }

  async function handleCancel() {
    setActionLoading(true)
    try {
      await workflowApi.cancelWorkflow(instanceId)
      message.success('已取消')
      notifyChangedAndClose()
    } catch (error) {
      message.error('操作失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setActionLoading(false)
    }
  }

  async function handleComplete() {
    setActionLoading(true)
    try {
      await workflowApi.completeWorkflow(instanceId)
      message.success('已标记完成')
      notifyChangedAndClose()
    } catch (error) {
      message.error('操作失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setActionLoading(false)
    }
  }

  async function handleResubmit() {
    setActionLoading(true)
    try {
      await workflowApi.resubmitWorkflow(instanceId)
      message.success('已重新提交')
      notifyChangedAndClose()
    } catch (error) {
      message.error('操作失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setActionLoading(false)
    }
  }

  async function handleDownload(file) {
    try {
      await workflowApi.downloadFile(instance.conversation_id, file.file_id, file.filename)
    } catch (error) {
      message.error('下载失败: ' + (error.response?.data?.detail || error.message))
    }
  }

  const isOwner = instance && meUserId && instance.requester_user_id === meUserId
  const statusMeta = instance ? workflowStatusMeta(instance.status) : null

  return (
    <Drawer
      title={instance ? `${instance.display_name} · #${instance.instance_id.slice(0, 8)}` : '工作流详情'}
      open={open}
      onClose={onClose}
      width={440}
      destroyOnHidden
    >
      {loading || !instance ? (
        <div style={{ textAlign: 'center', padding: '40px 0' }}><Spin /></div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
          <div>
            <Tag color={statusMeta.color}>{statusMeta.label}</Tag>
            <span style={{ marginLeft: 8, color: 'var(--text-tertiary, #999)' }}>
              提交于 {formatTs(instance.created_at)}
            </span>
          </div>

          <div>
            <div style={{ fontWeight: 600, marginBottom: 8 }}>申请内容</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {(template?.required_fields || []).map((f) => (
                <div key={f.key} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13 }}>
                  <span style={{ color: 'var(--text-tertiary, #888)' }}>{f.label}</span>
                  <span>{instance.fields[f.key] ?? '—'}</span>
                </div>
              ))}
            </div>
          </div>

          {template?.attachments_note && (
            <div>
              <div style={{ fontWeight: 600, marginBottom: 4 }}>材料提醒</div>
              <div style={{ fontSize: 13, color: 'var(--text-tertiary, #888)' }}>{template.attachments_note}</div>
            </div>
          )}

          <div>
            <div style={{ fontWeight: 600, marginBottom: 8 }}>关联材料</div>
            {files.length === 0 ? (
              <Empty description="暂无上传的材料" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {files.map((f) => (
                  <div key={f.file_id} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
                    <FileText size={14} />
                    <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {f.filename}
                    </span>
                    <Button size="small" type="text" icon={<Download size={14} />} onClick={() => handleDownload(f)} />
                  </div>
                ))}
              </div>
            )}
          </div>

          <div>
            <div style={{ fontWeight: 600, marginBottom: 8 }}>审批轨迹</div>
            <Timeline
              items={instance.history.map((h) => ({
                children: (
                  <div>
                    <div>{EVENT_LABEL[h.event] || h.event}</div>
                    {h.comment && <div style={{ fontSize: 12, color: 'var(--text-tertiary, #888)' }}>{h.comment}</div>}
                    <div style={{ fontSize: 12, color: 'var(--text-tertiary, #999)' }}>{formatTs(h.ts)}</div>
                  </div>
                ),
              }))}
            />
          </div>

          <Space wrap>
            {!isOwner && instance.status === 'pending_approval' && (
              <>
                <Button type="primary" loading={actionLoading} onClick={handleApprove}>通过</Button>
                <Button loading={actionLoading} onClick={() => { setComment(''); setReturnModalOpen(true) }}>打回</Button>
                <Button danger loading={actionLoading} onClick={() => { setComment(''); setRejectModalOpen(true) }}>驳回</Button>
              </>
            )}
            {isOwner && instance.status === 'pending_approval' && (
              <Button danger loading={actionLoading} onClick={handleCancel}>取消申请</Button>
            )}
            {isOwner && instance.status === 'returned_for_revision' && (
              <>
                <Button type="primary" loading={actionLoading} onClick={handleResubmit}>重新提交</Button>
                <Button danger loading={actionLoading} onClick={handleCancel}>取消申请</Button>
              </>
            )}
            {instance.status === 'approved' && (isOwner || !isOwner) && (
              <Button type="primary" loading={actionLoading} onClick={handleComplete}>标记办理完成</Button>
            )}
          </Space>

          {instance.conversation_id && (
            <a style={{ fontSize: 13 }} onClick={() => window.dispatchEvent(
              new CustomEvent('ragent:open-conversation', { detail: { conversationId: instance.conversation_id } })
            )}>
              在原对话中查看 →
            </a>
          )}
        </div>
      )}

      <Modal
        title="打回补充材料"
        open={returnModalOpen}
        onCancel={() => setReturnModalOpen(false)}
        onOk={submitReturn}
        confirmLoading={actionLoading}
        okText="确认打回"
        cancelText="取消"
      >
        <Input.TextArea
          rows={3}
          placeholder="写清楚缺什么材料，申请人会看到这句话"
          value={comment}
          onChange={(e) => setComment(e.target.value)}
        />
      </Modal>

      <Modal
        title="驳回申请"
        open={rejectModalOpen}
        onCancel={() => setRejectModalOpen(false)}
        onOk={submitReject}
        confirmLoading={actionLoading}
        okText="确认驳回"
        okButtonProps={{ danger: true }}
        cancelText="取消"
      >
        <p style={{ color: '#ef4444', fontSize: 13 }}>驳回后申请人需要重新发起，不可恢复。</p>
        <Input.TextArea
          rows={3}
          placeholder="驳回原因"
          value={comment}
          onChange={(e) => setComment(e.target.value)}
        />
      </Modal>
    </Drawer>
  )
}
