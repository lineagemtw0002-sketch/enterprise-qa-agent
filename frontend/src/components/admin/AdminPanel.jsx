import { Tabs } from 'antd'
import RoleManagement from './RoleManagement.jsx'
import UserRoleAssignment from './UserRoleAssignment.jsx'
import CompanyKbPermissions from './CompanyKbPermissions.jsx'
import AuditLogViewer from './AuditLogViewer.jsx'
import './AdminPanel.css'

export default function AdminPanel({ meProfile }) {
  const isPlatformAdmin = !!meProfile?.organization?.is_platform

  const items = [
    { key: 'users', label: '用户与角色分配', children: <UserRoleAssignment meProfile={meProfile} /> },
  ]
  // 角色的新建/重命名/删除是跨企业/平台级操作，企业管理员不给入口（角色是
  // 全平台共享的词表，没有按企业隔离，见 app.py 角色管理 API 旁的说明）——
  // 不是权限拒绝页，是压根不给入口，跟顶导航"管理后台"整个模块只有 isAdmin
  // 才露出来的做法一致；后端对应端点本来也只对平台管理员开放，这里只是不让
  // 人以为点了有用。工作流模板/组织管理/网关监控这三个原本也在这里的平台级
  // 页面已经搬去「运营仪表盘」用菜单切换了（同样只对 isPlatformAdmin 可见，
  // 见 TopNav.jsx 的 platformOnly），不再挂在「权限系统」下面。
  // 知识库权限不一样：只有企业管理员关心"本企业员工能看哪些知识库"，平台
  // 管理员不管理任何企业的知识库内容（见 UserRoleAssignment.jsx 同一处判断），
  // 所以这个入口反过来只给企业管理员，平台管理员用"角色管理"里更完整的
  // （能建角色）版本，不需要重复一个入口。
  if (isPlatformAdmin) {
    items.push({ key: 'roles', label: '角色管理', children: <RoleManagement /> })
  } else {
    items.push({ key: 'kb-permissions', label: '知识库权限', children: <CompanyKbPermissions /> })
  }
  // 审计日志：治理与合规，两层管理员都能碰，只是看到的范围不同（后端按
  // 调用者身份强制限定，见 audit_store.py/app.py admin_list_audit_logs），
  // 所以这里不像上面那样按 isPlatformAdmin 二选一，两边都给入口。
  items.push({ key: 'audit', label: '审计日志', children: <AuditLogViewer /> })

  return (
    <div className="admin-panel">
      <h2 className="module-title">权限系统</h2>

      {/* destroyOnHidden：antd Tabs 默认切走的 tab 不卸载组件，只是隐藏——
          「用户与角色分配」「知识库权限」这些页面的角色/知识库列表都是挂载时
          （useEffect(() => { loadAll() }, [])）拉一次接口存进 state，不卸载
          就不会重新拉，导致在「角色管理」新建/删除角色后切回「用户与角色
          分配」，看到的还是切走前那份旧列表，得整页刷新才能看到最新的。加
          这个之后每次切回某个 tab 都是重新挂载，天然会重新拉一次接口，不用
          给每个子页面单独加"tab 激活时刷新"的逻辑。 */}
      <Tabs className="admin-panel-tabs" defaultActiveKey="users" items={items} destroyOnHidden />
    </div>
  )
}
