import { Tabs } from 'antd'
import RoleManagement from './RoleManagement.jsx'
import UserRoleAssignment from './UserRoleAssignment.jsx'
import WorkflowTemplateManagement from './WorkflowTemplateManagement.jsx'
import OrganizationManagement from './OrganizationManagement.jsx'
import GatewayMonitor from './GatewayMonitor.jsx'
import './AdminPanel.css'

export default function AdminPanel({ meProfile }) {
  const isPlatformAdmin = !!meProfile?.organization?.is_platform

  const items = [
    { key: 'users', label: '用户与角色分配', children: <UserRoleAssignment meProfile={meProfile} /> },
  ]
  // 角色定义、工作流模板、组织/连接器管理都是跨企业/平台级操作，企业管理员
  // 只给"用户与角色分配"一个入口（管自己企业内的人）——不是权限拒绝页，是
  // 压根不给入口，跟顶导航"管理后台"整个模块只有 isAdmin 才露出来的做法一致；
  // 后端对应端点本来也只对平台管理员开放，这里只是不让人以为点了有用。
  if (isPlatformAdmin) {
    items.push(
      { key: 'roles', label: '角色管理', children: <RoleManagement /> },
      { key: 'workflows', label: '工作流管理', children: <WorkflowTemplateManagement /> },
      { key: 'organizations', label: '组织管理', children: <OrganizationManagement /> },
      { key: 'gateway', label: '网关', children: <GatewayMonitor /> },
    )
  }

  return (
    <div className="admin-panel">
      <h2 className="module-title">权限系统</h2>

      <Tabs className="admin-panel-tabs" defaultActiveKey="users" items={items} />
    </div>
  )
}
