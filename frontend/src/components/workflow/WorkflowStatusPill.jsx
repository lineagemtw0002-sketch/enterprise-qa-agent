import { Paperclip, X } from 'lucide-react'
import './WorkflowInputControls.css'

// chat-header 里常驻的"填写中"提示（work-flow-web.md 第 5.2 节）：文案完全由
// 后端每轮响应的 active_workflow 字段驱动，前端不在多轮之间自己累加进度。
//
// onUpload 是可选的：只有申请可能需要材料（病假证明、发票等）时才有意义露出这个
// 入口，具体上传逻辑（文件选择、调用上传接口）复用 App.jsx 里已有的
// uploadFile()，这里只负责触发，不重新实现一遍。
export default function WorkflowStatusPill({ activeWorkflow, onCancel, onUpload }) {
  if (!activeWorkflow) return null
  const { display_name, missing_count, total_count } = activeWorkflow
  const filled = Math.max(total_count - missing_count, 0)

  return (
    <div className="workflow-status-pill">
      <span>填写中：{display_name} · {filled}/{total_count} 项</span>
      {onUpload && (
        <button type="button" className="workflow-status-pill-upload" onClick={onUpload} title="上传材料（如证明、发票）">
          <Paperclip size={12} />
        </button>
      )}
      <button type="button" className="workflow-status-pill-cancel" onClick={onCancel} title="取消本次申请">
        <X size={12} />
      </button>
    </div>
  )
}
