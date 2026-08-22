import TopNav from './TopNav.jsx'
import './AppShell.css'

// 登录后所有界面（智能问答 / 工作流 / 权限系统 / 智能运维）共用的外壳：
// 顶部导航固定，下面是同一张带圆角阴影的卡片，具体模块内容作为 children 传入。
export default function AppShell({
  view,
  onNavigate,
  isAdmin,
  isPlatformAdmin,
  currentUsername,
  meProfile,
  onLogout,
  onOpenChangePassword,
  onWorkflowDeepLink,
  children,
}) {
  return (
    <div className="app-shell">
      <TopNav
        view={view}
        onNavigate={onNavigate}
        isAdmin={isAdmin}
        isPlatformAdmin={isPlatformAdmin}
        currentUsername={currentUsername}
        meProfile={meProfile}
        onLogout={onLogout}
        onOpenChangePassword={onOpenChangePassword}
        onWorkflowDeepLink={onWorkflowDeepLink}
      />
      <div className="shell-canvas">
        <div className="shell-card">{children}</div>
      </div>
    </div>
  )
}
