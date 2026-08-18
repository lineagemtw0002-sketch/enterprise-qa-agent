import { Wrench, CalendarOff, Plane, Receipt, ClipboardList } from 'lucide-react'

// workflow_type -> 图标/主题色，跟 App.jsx 里 KB_META 给知识库配图标颜色的思路一致
// （work-flow-web.md 4.1 节）。没在表里的类型（管理员后台自定义的新模板）
// 原样兜底成通用图标。
const WORKFLOW_TYPE_META = {
  laptop_repair: { icon: Wrench, color: '#3b82f6' },
  leave_request: { icon: CalendarOff, color: '#f59e0b' },
  business_trip: { icon: Plane, color: '#10b981' },
  expense_reimbursement: { icon: Receipt, color: '#8b5cf6' },
}
const WORKFLOW_TYPE_FALLBACK = { icon: ClipboardList, color: '#6b7280' }

export function workflowTypeMeta(workflowType) {
  return WORKFLOW_TYPE_META[workflowType] || WORKFLOW_TYPE_FALLBACK
}

// 状态 -> 展示文案 + antd Tag color
export const WORKFLOW_STATUS_META = {
  pending_approval: { label: '待审批', color: 'blue' },
  returned_for_revision: { label: '已打回', color: 'orange' },
  approved: { label: '已通过', color: 'green' },
  rejected: { label: '已驳回', color: 'red' },
  completed: { label: '已完成', color: 'default' },
  cancelled: { label: '已取消', color: 'default' },
}

export function workflowStatusMeta(status) {
  return WORKFLOW_STATUS_META[status] || { label: status, color: 'default' }
}
