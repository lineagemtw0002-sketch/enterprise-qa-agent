import { useEffect, useRef, useState } from 'react'
import { Table, Tag, Button, Tooltip, Empty, message } from 'antd'
import { RefreshCw } from 'lucide-react'
import * as adminApi from '../../api/admin.js'
import { HealthBadge } from './connectorHealth.jsx'

const CAPABILITY_LABELS = {
  knowledge_base: '知识库检索',
  attendance: '考勤查询',
}

const POLL_INTERVAL_MS = 15000

function formatTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleString('zh-CN') : '—'
}

// 平台管理员的"网关"总览：横向列出所有企业接入的外部微服务连接器（知识库/
// 考勤），每行是一个正在运行（或应该在运行）的微服务，展示它属于哪家企业、
// 现在通不通、调用了多少次、失败了多少次——对应需求"查看所有微服务，并属于
// 哪个企业，微服务的流量、调用次数、失败次数"。
export default function GatewayMonitor() {
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(false)
  const pollRef = useRef(null)

  async function load(silent) {
    if (!silent) setLoading(true)
    try {
      setRows(await adminApi.listGatewayConnectors())
    } catch (error) {
      if (!silent) message.error('加载网关数据失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      if (!silent) setLoading(false)
    }
  }

  useEffect(() => {
    load(false)
    pollRef.current = setInterval(() => load(true), POLL_INTERVAL_MS)
    return () => clearInterval(pollRef.current)
  }, [])

  const columns = [
    { title: '所属企业', dataIndex: 'org_name', key: 'org_name', width: 160 },
    {
      title: '能力',
      dataIndex: 'capability',
      key: 'capability',
      width: 120,
      render: (cap) => CAPABILITY_LABELS[cap] || cap,
    },
    {
      title: 'Endpoint（微服务地址）',
      dataIndex: 'endpoint',
      key: 'endpoint',
      render: (endpoint) => <code>{endpoint || '—'}</code>,
    },
    {
      title: '类型',
      dataIndex: 'connector_type',
      key: 'connector_type',
      width: 130,
      render: (t) => <Tag color="blue">{t}</Tag>,
    },
    {
      title: '状态',
      dataIndex: 'health_status',
      key: 'health_status',
      width: 110,
      render: (status) => <HealthBadge status={status} />,
    },
    {
      title: '调用次数',
      dataIndex: 'call_count',
      key: 'call_count',
      width: 90,
      sorter: (a, b) => a.call_count - b.call_count,
    },
    {
      title: '失败次数',
      dataIndex: 'failure_count',
      key: 'failure_count',
      width: 90,
      sorter: (a, b) => a.failure_count - b.failure_count,
      render: (n) => (n > 0 ? <span style={{ color: '#ff4d4f' }}>{n}</span> : n),
    },
    {
      title: '失败率',
      key: 'failure_rate',
      width: 90,
      render: (_, row) =>
        row.call_count > 0 ? `${((row.failure_count / row.call_count) * 100).toFixed(1)}%` : '—',
    },
    {
      title: '最近调用',
      dataIndex: 'last_called_at',
      key: 'last_called_at',
      width: 170,
      render: formatTime,
    },
    {
      title: '最近延迟',
      dataIndex: 'last_latency_ms',
      key: 'last_latency_ms',
      width: 100,
      render: (ms) => (ms != null ? `${ms.toFixed(0)} ms` : '—'),
    },
    {
      title: '最近错误',
      dataIndex: 'last_error',
      key: 'last_error',
      render: (err) =>
        err ? (
          <Tooltip title={err}>
            <span style={{ color: '#ff4d4f' }}>{err.length > 24 ? err.slice(0, 24) + '…' : err}</span>
          </Tooltip>
        ) : (
          '—'
        ),
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <p style={{ margin: 0, color: 'var(--text-tertiary, #888)' }}>
          所有企业接入的外部微服务连接器（知识库/考勤），内置实现不算独立微服务，不在此列出。每 15 秒自动刷新一次。
        </p>
        <Button icon={<RefreshCw size={14} />} onClick={() => load(false)}>刷新</Button>
      </div>

      <Table
        rowKey="connector_id"
        columns={columns}
        dataSource={rows}
        loading={loading}
        pagination={false}
        locale={{ emptyText: <Empty description="还没有企业接入外部微服务" /> }}
      />
    </div>
  )
}
