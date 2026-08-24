import { Tabs } from 'antd'
import RoleManagement from './RoleManagement.jsx'
import UserRoleAssignment from './UserRoleAssignment.jsx'
import CompanyRoleManagement from './CompanyRoleManagement.jsx'
import CompanyKbManagement from './CompanyKbManagement.jsx'
import CompanyWorkflowApprovers from './CompanyWorkflowApprovers.jsx'
import AuditLogViewer from './AuditLogViewer.jsx'
import './AdminPanel.css'

export default function AdminPanel({ meProfile }) {
  const isPlatformAdmin = !!meProfile?.organization?.is_platform

  const items = [
    { key: 'users', label: '用户管理', children: <UserRoleAssignment meProfile={meProfile} /> },
  ]
  // 全局角色（系统权限档位 + 跨企业共用的部门身份）的新建/重命名/删除是
  // 跨企业/平台级操作，企业管理员不给入口——不是权限拒绝页，是压根不给
  // 入口，跟顶导航"管理后台"整个模块只有 isAdmin 才露出来的做法一致；后端
  // 对应端点本来也只对平台管理员开放，这里只是不让人以为点了有用。工作流
  // 模板/组织管理/网关监控这三个原本也在这里的平台级页面已经搬去「运营
  // 仪表盘」用菜单切换了（同样只对 isPlatformAdmin 可见，见 TopNav.jsx 的
  // platformOnly），不再挂在「权限系统」下面。
  //
  // 企业管理员这边：「角色管理」（CompanyRoleManagement.jsx）管自己企业的
  // 角色——2026-08-23 起角色直接携带知识库权限（role_store.py），不再是
  // 独立于身份的另一套实体；「知识库管理」（CompanyKbManagement.jsx）管
  // 知识库本身（新增/删/分页查看数据）；「审批设置」（CompanyWorkflowApprovers.jsx）
  // 管"哪类工作流由哪个角色审批"——工作流跟角色/知识库一样是"企业内部的
  // 事"，同一个工作流类型在不同企业可以配不同的审批角色，只能用本企业自己
  // 建的角色，见 workflow_store.py 顶部说明。这几个页面拆成单独的 tab——
  // 各自的建/删/配置和查看数据是不同的事，混在一个页面里之前列表越来越长、
  // 职责也不清楚。平台管理员不管理任何企业的知识库/工作流审批内容（见
  // UserRoleAssignment.jsx 同一处判断），这几个 tab 都不给平台管理员。
  if (isPlatformAdmin) {
    items.push({ key: 'roles', label: '角色管理', children: <RoleManagement /> })
  } else {
    items.push({ key: 'company-roles', label: '角色管理', children: <CompanyRoleManagement /> })
    items.push({ key: 'kb-management', label: '知识库管理', children: <CompanyKbManagement /> })
    items.push({ key: 'workflow-approvers', label: '审批设置', children: <CompanyWorkflowApprovers /> })
  }
  // 审计日志：治理与合规，两层管理员都能碰，只是看到的范围不同（后端按
  // 调用者身份强制限定，见 audit_store.py/app.py admin_list_audit_logs），
  // 所以这里不像上面那样按 isPlatformAdmin 二选一，两边都给入口。
  items.push({ key: 'audit', label: '审计日志', children: <AuditLogViewer /> })

  return (
    <div className="admin-panel">
      <h2 className="module-title">权限系统</h2>

      {/* destroyOnHidden：antd Tabs 默认切走的 tab 不卸载组件，只是隐藏——
          这几个页面的用户/角色/知识库列表都是挂载时（useEffect(() => {
          loadAll() }, [])）拉一次接口存进 state，不卸载就不会重新拉，导致
          在「角色管理」新建/删除角色后切回「用户管理」，看到的还是切走前
          那份旧列表，得整页刷新才能看到最新的。加这个之后每次切回某个 tab
          都是重新挂载，天然会重新拉一次接口，不用给每个子页面单独加"tab
          激活时刷新"的逻辑。 */}
      <Tabs className="admin-panel-tabs" defaultActiveKey="users" items={items} destroyOnHidden />
    </div>
  )
}
