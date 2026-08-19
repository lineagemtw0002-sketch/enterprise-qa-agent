import { Tooltip } from 'antd'
import { CheckCircle2, XCircle, CircleSlash, MinusCircle } from 'lucide-react'

// 跟后端 app.py::_check_connector_health 的取值一一对应，被
// OrganizationManagement（连接器配置抽屉）和 GatewayMonitor（网关监控页）共用。
export const HEALTH_META = {
  connected: { icon: CheckCircle2, color: '#52c41a', label: '连接成功' },
  unreachable: { icon: XCircle, color: '#ff4d4f', label: '连接失败' },
  disabled: { icon: CircleSlash, color: '#8c8c8c', label: '已停用' },
  internal: { icon: MinusCircle, color: '#8c8c8c', label: '内置实现（无需外部连接）' },
}

export function HealthBadge({ status }) {
  const meta = HEALTH_META[status] || HEALTH_META.unreachable
  const Icon = meta.icon
  return (
    <Tooltip title={meta.label}>
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, color: meta.color }}>
        <Icon size={14} />
        {meta.label}
      </span>
    </Tooltip>
  )
}
