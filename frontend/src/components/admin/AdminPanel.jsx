import { Tabs } from 'antd'
import RoleManagement from './RoleManagement.jsx'
import UserRoleAssignment from './UserRoleAssignment.jsx'
import WorkflowTemplateManagement from './WorkflowTemplateManagement.jsx'
import './AdminPanel.css'

export default function AdminPanel() {
  return (
    <div className="admin-panel">
      <h2 className="module-title">权限系统</h2>

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
