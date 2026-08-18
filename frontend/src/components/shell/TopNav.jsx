import { Popover } from 'antd'
import {
  Sparkles, ChevronDown, MessageSquare, ListChecks, ShieldCheck, Activity,
  Cpu, Clock, Package, Scale, BookOpen, Sparkle, Files as FilesIcon,
} from 'lucide-react'
import NotificationBell from '../workflow/NotificationBell.jsx'
import './TopNav.css'

// 知识库 slug -> 中文展示名 + 图标 + 主题色（从 App.jsx 挪过来，只有个人信息卡
// 里的"可访问知识库"用得到，搬到顶部导航就不用在 App.jsx 里保留一份了）
const KB_META = {
  default: { label: '通用知识库', icon: BookOpen, color: '#6b7280' },
  it_kb: { label: 'IT 知识库', icon: Cpu, color: '#3b82f6' },
  attendance_kb: { label: '考勤知识库', icon: Clock, color: '#f59e0b' },
  logistics_kb: { label: '后勤知识库', icon: Package, color: '#10b981' },
  legal_kb: { label: '法务知识库', icon: Scale, color: '#ef4444' },
}
const KB_META_FALLBACK = { icon: FilesIcon, color: '#6b7280' }
const KB_META_UNLIMITED = { label: '不限', icon: Sparkle, color: '#8b5cf6' }

function kbMeta(slug) {
  return { ...KB_META_FALLBACK, label: slug, ...KB_META[slug] }
}

function KbTag({ slug }) {
  const meta = slug === '*' ? KB_META_UNLIMITED : kbMeta(slug)
  const Icon = meta.icon
  return (
    <span className="tag tag--kb" style={{ '--kb-color': meta.color }}>
      <Icon size={12} strokeWidth={2.25} />
      {meta.label}
    </span>
  )
}

// 用户名 hash 出一个稳定的颜色，同一个用户名每次颜色都一样，不用后端存头像
function avatarColor(username) {
  if (!username) return '#94a3b8'
  let hash = 0
  for (let i = 0; i < username.length; i++) {
    hash = username.charCodeAt(i) + ((hash << 5) - hash)
  }
  const hue = Math.abs(hash) % 360
  return `hsl(${hue}, 55%, 45%)`
}

function avatarInitial(username) {
  return username ? username.charAt(0).toUpperCase() : '?'
}

function formatDate(timestamp) {
  if (!timestamp) return ''
  return new Date(timestamp * 1000).toLocaleDateString('zh-CN')
}

const MODULES = [
  { key: 'chat', label: '智能问答', icon: MessageSquare },
  { key: 'workflow', label: '工作流', icon: ListChecks },
  { key: 'admin', label: '权限系统', icon: ShieldCheck, adminOnly: true },
  { key: 'ops', label: '智能运维', icon: Activity, soon: true },
]

// 顶部导航：登录后所有界面共用的壳——模块切换 + 通知 + 个人信息，替代原来
// 只有聊天页侧边栏才有、且要点开头像弹层才能找到"工作流/管理后台"入口的做法。
export default function TopNav({
  view,
  onNavigate,
  isAdmin,
  currentUsername,
  meProfile,
  onLogout,
  onOpenChangePassword,
  onWorkflowDeepLink,
}) {
  const profileCard = (
    <div className="profile-card">
      <div className="profile-card-header">
        <div className="user-avatar user-avatar--lg" style={{ background: avatarColor(currentUsername) }}>
          {avatarInitial(currentUsername)}
        </div>
        <div>
          <div className="profile-name">{currentUsername}</div>
          {meProfile && <div className="profile-joined">加入于 {formatDate(meProfile.created_at)}</div>}
        </div>
      </div>

      <div className="profile-section">
        <div className="profile-section-label">可访问知识库</div>
        <div className="profile-collections">
          {meProfile?.allowed_collections?.includes('*') ? (
            <KbTag slug="*" />
          ) : meProfile?.allowed_collections?.length ? (
            meProfile.allowed_collections.map((c) => <KbTag key={c} slug={c} />)
          ) : (
            <span className="profile-empty-hint">仅自己的对话</span>
          )}
        </div>
      </div>

      <hr className="profile-divider" />

      <div className="profile-actions">
        <button className="dialog-btn" style={{ width: '100%', marginLeft: 0 }} onClick={onOpenChangePassword}>
          修改密码
        </button>
        <button className="dialog-btn dialog-btn--danger" style={{ width: '100%', marginLeft: 0 }} onClick={onLogout}>
          退出登录
        </button>
      </div>
    </div>
  )

  return (
    <nav className="top-nav">
      <div className="nav-logo">
        <Sparkles size={20} color="var(--accent)" />
        <span className="nav-logo-text">RAG Agent</span>
      </div>

      <div className="nav-modules">
        {MODULES.filter((m) => !m.adminOnly || isAdmin).map((m) => (
          <div
            key={m.key}
            className={[
              'nav-item',
              view === m.key ? 'active' : '',
              m.soon ? 'soon' : '',
            ].filter(Boolean).join(' ')}
            onClick={() => onNavigate(m.key)}
          >
            <m.icon size={16} />
            {m.label}
            {m.soon && <span className="nav-soon">即将上线</span>}
          </div>
        ))}
      </div>

      <div className="nav-right">
        <NotificationBell
          onNavigate={(link) => {
            const instanceId = link?.startsWith('workflow:') ? link.slice('workflow:'.length) : null
            if (!instanceId) return
            onWorkflowDeepLink(instanceId)
          }}
        />
        <Popover content={profileCard} trigger="click" placement="bottomRight" overlayClassName="profile-popover">
          <div className="nav-user">
            <div className="nav-avatar" style={{ background: avatarColor(currentUsername) }}>
              {avatarInitial(currentUsername)}
            </div>
            <span className="nav-username">{currentUsername}</span>
            <ChevronDown size={13} className="nav-user-arrow" />
          </div>
        </Popover>
      </div>
    </nav>
  )
}
