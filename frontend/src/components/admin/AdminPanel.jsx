import { Tabs } from 'antd'
import RoleManagement from './RoleManagement.jsx'
import UserRoleAssignment from './UserRoleAssignment.jsx'
import WorkflowTemplateManagement from './WorkflowTemplateManagement.jsx'
import OrganizationManagement from './OrganizationManagement.jsx'
import './AdminPanel.css'

export default function AdminPanel({ meProfile }) {
  const isPlatformAdmin = !!meProfile?.organization?.is_platform

  const items = [
    { key: 'users', label: '用户与角色分配', children: <UserRoleAssignment meProfile={meProfile} /> },
    { key: 'roles', label: '角色管理', children: <RoleManagement /> },
    { key: 'workflows', label: '工作流管理', children: <WorkflowTemplateManagement /> },
  ]
  // 组织管理只有平台管理员看得到——不是权限拒绝页，是压根不给入口，
  // 跟顶导航"管理后台"整个模块只有 isAdmin 才露出来的做法一致。
  if (isPlatformAdmin) {
    items.push({ key: 'organizations', label: '组织管理', children: <OrganizationManagement /> })
  }

  return (
    <div className="admin-panel">
      <h2 className="module-title">权限系统</h2>

      <Tabs className="admin-panel-tabs" defaultActiveKey="users" items={items} />
    </div>
  )
}
