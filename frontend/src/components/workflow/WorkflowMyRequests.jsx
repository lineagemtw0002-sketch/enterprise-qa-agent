import { useEffect, useState } from 'react'
import { Table, Tag, Button, Space, Empty, message } from 'antd'
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
          {row.status === 'returned_for_revision' && (
            <Button
              size="small"
              type="primary"
              onClick={() => onGoToChat?.(row.conversation_id)}
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
        onChanged={load}
      />
    </div>
  )
}
