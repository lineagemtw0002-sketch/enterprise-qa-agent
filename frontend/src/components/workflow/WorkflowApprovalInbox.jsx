import { useEffect, useState } from 'react'
import { Table, Tag, Button, Empty, message } from 'antd'
import * as workflowApi from '../../api/workflow.js'
import { workflowTypeMeta, workflowStatusMeta } from './workflowMeta.js'
import WorkflowDetailDrawer from './WorkflowDetailDrawer.jsx'

export default function WorkflowApprovalInbox({ meUserId }) {
  const [instances, setInstances] = useState([])
  const [loading, setLoading] = useState(false)
  const [detailId, setDetailId] = useState(null)

  async function load() {
    setLoading(true)
    try {
      const data = await workflowApi.listPendingApprovalWorkflows()
      setInstances(data)
    } catch (error) {
      message.error('加载待审批列表失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const columns = [
    {
      title: '申请人',
      dataIndex: 'requester_username',
      key: 'requester_username',
    },
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
      title: '操作',
      key: 'actions',
      render: (_, row) => (
        <Button size="small" type="primary" onClick={() => setDetailId(row.instance_id)}>
          处理
        </Button>
      ),
    },
  ]

  return (
    <div>
      <p style={{ margin: '0 0 16px', color: 'var(--text-tertiary, #888)' }}>
        以下是待你审批的申请；材料齐不齐由你判断，通过或打回都会通知申请人。
      </p>

      <Table
        rowKey="instance_id"
        columns={columns}
        dataSource={instances}
        loading={loading}
        pagination={false}
        locale={{ emptyText: <Empty description="暂无待审批的申请" /> }}
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
