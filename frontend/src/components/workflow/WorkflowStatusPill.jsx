import { X } from 'lucide-react'
import './WorkflowInputControls.css'

// chat-header 里常驻的"填写中"提示（work-flow-web.md 第 5.2 节）：文案完全由
// 后端每轮响应的 active_workflow 字段驱动，前端不在多轮之间自己累加进度。
export default function WorkflowStatusPill({ activeWorkflow, onCancel }) {
  if (!activeWorkflow) return null
  const { display_name, missing_count, total_count } = activeWorkflow
  const filled = Math.max(total_count - missing_count, 0)

  return (
    <div className="workflow-status-pill">
      <span>填写中：{display_name} · {filled}/{total_count} 项</span>
      <button type="button" className="workflow-status-pill-cancel" onClick={onCancel} title="取消本次申请">
        <X size={12} />
      </button>
    </div>
  )
}
