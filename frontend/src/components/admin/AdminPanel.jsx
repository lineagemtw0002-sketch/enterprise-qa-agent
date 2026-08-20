import { Tabs } from 'antd'
import RoleManagement from './RoleManagement.jsx'
import UserRoleAssignment from './UserRoleAssignment.jsx'
import CompanyKbPermissions from './CompanyKbPermissions.jsx'
import WorkflowTemplateManagement from './WorkflowTemplateManagement.jsx'
import OrganizationManagement from './OrganizationManagement.jsx'
import GatewayMonitor from './GatewayMonitor.jsx'
import './AdminPanel.css'

export default function AdminPanel({ meProfile }) {
  const isPlatformAdmin = !!meProfile?.organization?.is_platform

  const items = [
    { key: 'users', label: '用户与角色分配', children: <UserRoleAssignment meProfile={meProfile} /> },
  ]
  // 角色的新建/重命名/删除、工作流模板、组织/连接器管理都是跨企业/平台级
  // 操作，企业管理员不给入口（角色是全平台共享的词表，没有按企业隔离，见
  // app.py 角色管理 API 旁的说明）——不是权限拒绝页，是压根不给入口，跟顶
  // 导航"管理后台"整个模块只有 isAdmin 才露出来的做法一致；后端对应端点
  // 本来也只对平台管理员开放，这里只是不让人以为点了有用。
  // 知识库权限不一样：只有企业管理员关心"本企业员工能看哪些知识库"，平台
  // 管理员不管理任何企业的知识库内容（见 UserRoleAssignment.jsx 同一处判断），
  // 所以这个入口反过来只给企业管理员，平台管理员用"角色管理"里更完整的
  // （能建角色）版本，不需要重复一个入口。
  if (isPlatformAdmin) {
    items.push(
      { key: 'roles', label: '角色管理', children: <RoleManagement /> },
      { key: 'workflows', label: '工作流管理', children: <WorkflowTemplateManagement /> },
      { key: 'organizations', label: '组织管理', children: <OrganizationManagement /> },
      { key: 'gateway', label: '网关', children: <GatewayMonitor /> },
    )
  } else {
    items.push({ key: 'kb-permissions', label: '知识库权限', children: <CompanyKbPermissions /> })
  }

  return (
    <div className="admin-panel">
      <h2 className="module-title">权限系统</h2>

      <Tabs className="admin-panel-tabs" defaultActiveKey="users" items={items} />
    </div>
  )
}
