import { useEffect, useState } from 'react'
import { Tabs } from 'antd'
import * as workflowApi from '../../api/workflow.js'
import WorkflowMyRequests from './WorkflowMyRequests.jsx'
import WorkflowApprovalInbox from './WorkflowApprovalInbox.jsx'
import WorkflowDetailDrawer from './WorkflowDetailDrawer.jsx'
import './WorkflowPanel.css'

// "待我审批" Tab 是条件渲染的：只有当前用户至少持有一个模板的审批角色时才显示
// （work-flow-web.md 第 3 节）——不是"看到但空空如也"，是压根不显示。
export default function WorkflowPanel({ meUserId, onGoToChat, deepLinkInstanceId, onDeepLinkConsumed }) {
  const [isApprover, setIsApprover] = useState(false)
  const [deepLinkOpen, setDeepLinkOpen] = useState(false)

  useEffect(() => {
    workflowApi.listApprovableWorkflowTypes()
      .then((types) => setIsApprover((types || []).length > 0))
      .catch(() => setIsApprover(false))
  }, [])

  useEffect(() => {
    setDeepLinkOpen(!!deepLinkInstanceId)
  }, [deepLinkInstanceId])

  function closeDeepLink() {
    setDeepLinkOpen(false)
    onDeepLinkConsumed?.()
  }

  const items = [
    {
      key: 'mine',
      label: '我发起的',
      children: <WorkflowMyRequests meUserId={meUserId} onGoToChat={onGoToChat} />,
    },
  ]
  if (isApprover) {
    items.push({
      key: 'approval',
      label: '待我审批',
      children: <WorkflowApprovalInbox meUserId={meUserId} />,
    })
  }

  return (
    <div className="workflow-panel">
      <h2 className="module-title">工作流</h2>

      <Tabs className="workflow-panel-tabs" defaultActiveKey="mine" items={items} />

      {deepLinkInstanceId && (
        <WorkflowDetailDrawer
          instanceId={deepLinkInstanceId}
          open={deepLinkOpen}
          onClose={closeDeepLink}
          meUserId={meUserId}
          onChanged={() => {}}
        />
      )}
    </div>
  )
}
