import { useEffect, useState } from 'react'
import { Table, Tag, Button, Space, Empty, message } from 'antd'
import { Paperclip } from 'lucide-react'
import * as workflowApi from '../../api/workflow.js'
import { workflowTypeMeta, workflowStatusMeta } from './workflowMeta.js'
import WorkflowDetailDrawer from './WorkflowDetailDrawer.jsx'

export default function WorkflowMyRequests({ meUserId, onGoToChat }) {
  const [instances, setInstances] = useState([])
  const [loading, setLoading] = useState(false)
  const [detailId, setDetailId] = useState(null)

  async function load() {
    setLoading(true)
    try {
      const data = await workflowApi.listMyWorkflows()
      setInstances(data)
    } catch (error) {
      message.error('加载我发起的工作流失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const columns = [
    {
      title: '流程类型',
      dataIndex: 'workflow_type',
      key: 'workflow_type',
      render: (type, row) => {
        const meta = workflowTypeMeta(type)
        const Icon = meta.icon
        return (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            <Icon size={14} color={meta.color} />
            {row.display_name}
          </span>
        )
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      render: (status) => {
        const meta = workflowStatusMeta(status)
        return <Tag color={meta.color}>{meta.label}</Tag>
      },
    },
    {
      title: '审批材料',
      dataIndex: 'attachment_count',
      key: 'attachment_count',
      render: (count, row) => (
        <span
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 4, cursor: 'pointer',
            color: count > 0 ? 'var(--text-secondary, #555)' : 'var(--text-tertiary, #aaa)',
          }}
          onClick={() => setDetailId(row.instance_id)}
        >
          <Paperclip size={13} />
          {count > 0 ? `${count} 份` : '无'}
        </span>
      ),
    },
    {
      title: '提交时间',
      dataIndex: 'created_at',
      key: 'created_at',
      render: (ts) => new Date(ts * 1000).toLocaleString('zh-CN'),
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      key: 'updated_at',
      render: (ts) => new Date(ts * 1000).toLocaleString('zh-CN'),
    },
    {
      title: '操作',
      key: 'actions',
      render: (_, row) => (
        <Space>
          <Button size="small" onClick={() => setDetailId(row.instance_id)}>查看详情</Button>
          {/* 审批通过之前（pending_approval / returned_for_revision）都能补充材料，
             不用等被打回才能传。打开详情抽屉直接传，不用跳转到对话窗口再自己找
             上传入口——WorkflowDetailDrawer 里"关联材料"区域已经带了上传按钮
             （见 canUpload 那段），抽屉底部仍留着"在原对话中查看 →"，想去聊天
             里继续对话也走得通。approve 之后材料就不再影响这条申请，不提供
             这个入口。 */}
          {['pending_approval', 'returned_for_revision'].includes(row.status) && (
            <Button
              size="small"
              type="primary"
              onClick={() => setDetailId(row.instance_id)}
            >
              去补充材料
            </Button>
          )}
        </Space>
      ),
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <p style={{ margin: 0, color: 'var(--text-tertiary, #888)' }}>
          发起新的申请，直接在聊天里说就行——AI 会引导你把信息补齐。
        </p>
        <Button type="primary" onClick={() => onGoToChat?.()}>去聊天里发起 →</Button>
      </div>

      <Table
        rowKey="instance_id"
        columns={columns}
        dataSource={instances}
        loading={loading}
        pagination={false}
        locale={{ emptyText: <Empty description="还没有发起过任何工作流申请" /> }}
      />

      <WorkflowDetailDrawer
        instanceId={detailId}
        open={!!detailId}
        onClose={() => setDetailId(null)}
        meUserId={meUserId}
        mode="owner"
        onChanged={load}
      />
    </div>
  )
}
