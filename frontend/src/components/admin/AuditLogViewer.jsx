import { useEffect, useState } from 'react'
import { Table, Tag, Select, Button, Empty, message, Popover } from 'antd'
import { RefreshCw, FileText } from 'lucide-react'
import * as adminApi from '../../api/admin.js'

// 操作类型 -> 展示标签，覆盖 app.py 里所有会调用 `_audit_log(...)` 的落点
// （管理后台变更操作 + 工具调用），新增审计动作类型时在这里补一行就够了，
// 不认识的 action 会原样展示 action 字符串，不会报错/漏行。
const ACTION_LABELS = {
  tool_call: '工具调用',
  create_user: '创建用户',
  delete_user: '删除用户',
  set_user_roles: '调整用户角色',
  create_organization: '创建组织',
  upsert_connector: '配置连接器',
  create_role: '创建角色',
  update_role: '更新角色',
  delete_role: '删除角色',
  create_collection: '创建知识库',
}

const ACTION_OPTIONS = [
  { label: '全部操作', value: '' },
  ...Object.entries(ACTION_LABELS).map(([value, label]) => ({ label, value })),
]

function formatTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleString('zh-CN') : '—'
}

// 「审计日志」——治理与合规：记录谁在何时对哪个资源做了什么，覆盖管理后台
// 变更操作（建/删用户、改角色、配连接器……）和工具调用（知识库检索/考勤
// 查询/工作流操作）两类事件（见后端 audit_store.py）。平台管理员能看全平台
// 记录，企业管理员只能看自己企业的——不是前端过滤，是后端按调用者身份强制
// 限定查询范围（见 app.py admin_list_audit_logs）。挂在「权限系统」下面而不是
// 「运营仪表盘」，因为企业管理员也要看，跟运营仪表盘"只对平台两层管理员开放"
// 的边界不一样。
export default function AuditLogViewer() {
  const [logs, setLogs] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [action, setAction] = useState('')
  const [page, setPage] = useState(1)
  const pageSize = 20

  async function load() {
    setLoading(true)
    try {
      const data = await adminApi.listAuditLogs({
        action: action || undefined,
        limit: pageSize,
        offset: (page - 1) * pageSize,
      })
      setLogs(data.items)
      setTotal(data.total)
    } catch (error) {
      message.error('加载审计日志失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [action, page])

  const columns = [
    { title: '时间', dataIndex: 'created_at', key: 'created_at', width: 170, render: formatTime },
    {
      title: '操作人',
      key: 'actor',
      width: 140,
      render: (_, row) => row.username || row.user_id || '—',
    },
    {
      title: '操作',
      dataIndex: 'action',
      key: 'action',
      width: 110,
      render: (a) => <Tag color={a === 'tool_call' ? 'blue' : 'purple'}>{ACTION_LABELS[a] || a}</Tag>,
    },
    { title: '资源类型', dataIndex: 'resource_type', key: 'resource_type', width: 160 },
    {
      title: '资源',
      dataIndex: 'resource_id',
      key: 'resource_id',
      width: 140,
      render: (v) => v || '—',
    },
    {
      title: '所属企业',
      dataIndex: 'org_name',
      key: 'org_name',
      width: 140,
      render: (v, row) => v || row.org_id || '—',
    },
    {
      title: '结果',
      dataIndex: 'success',
      key: 'success',
      width: 80,
      render: (ok) => (ok ? <Tag color="success">成功</Tag> : <Tag color="error">失败</Tag>),
    },
    {
      title: '详情',
      key: 'detail',
      width: 70,
      render: (_, row) => (
        <Popover
          trigger="click"
          title="详情"
          content={<pre style={{ maxWidth: 360, maxHeight: 240, overflow: 'auto', margin: 0, fontSize: 12 }}>{JSON.stringify(row.detail, null, 2)}</pre>}
        >
          <FileText size={14} style={{ cursor: 'pointer', color: 'var(--text-tertiary, #888)' }} />
        </Popover>
      ),
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <p style={{ margin: 0, color: 'var(--text-tertiary, #888)' }}>
          谁在何时对哪个资源做了什么——覆盖管理后台变更操作和工具调用（知识库检索/考勤查询/工作流操作）。
          企业管理员只能看到本企业的记录。
        </p>
        <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexShrink: 0 }}>
          <Select
            options={ACTION_OPTIONS}
            value={action}
            onChange={(v) => { setAction(v); setPage(1) }}
            style={{ width: 160 }}
          />
          <Button icon={<RefreshCw size={14} />} onClick={load}>刷新</Button>
        </div>
      </div>

      <Table
        rowKey="audit_id"
        columns={columns}
        dataSource={logs}
        loading={loading}
        pagination={{
          current: page,
          pageSize,
          total,
          onChange: setPage,
          showTotal: (t) => `共 ${t} 条`,
        }}
        scroll={{ x: 900 }}
        locale={{ emptyText: <Empty description="暂无审计记录" /> }}
      />
    </div>
  )
}
