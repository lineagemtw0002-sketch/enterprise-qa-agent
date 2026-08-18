import { Tabs } from 'antd'
import { ArrowLeft } from 'lucide-react'
import RoleManagement from './RoleManagement.jsx'
import UserRoleAssignment from './UserRoleAssignment.jsx'
import WorkflowTemplateManagement from './WorkflowTemplateManagement.jsx'
import './AdminPanel.css'

export default function AdminPanel({ onBack }) {
  return (
    <div className="admin-panel">
      <div className="admin-panel-header">
        <button className="admin-back-btn" onClick={onBack}>
          <ArrowLeft size={16} />
          返回对话
        </button>
        <h2>管理后台</h2>
      </div>

      <Tabs
        className="admin-panel-tabs"
        defaultActiveKey="users"
        items={[
          { key: 'users', label: '用户与角色分配', children: <UserRoleAssignment /> },
          { key: 'roles', label: '角色管理', children: <RoleManagement /> },
          { key: 'workflows', label: '工作流管理', children: <WorkflowTemplateManagement /> },
        ]}
      />
    </div>
  )
}
