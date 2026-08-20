import { Popover } from 'antd'
import {
  Sparkles, ChevronDown, MessageSquare, ListChecks, ShieldCheck, Activity,
  Cpu, Clock, Package, Scale, BookOpen, Sparkle, Files as FilesIcon, Building2,
  Users, Wallet, Wrench, Megaphone, Code2, HeartHandshake,
} from 'lucide-react'
import NotificationBell from '../workflow/NotificationBell.jsx'
import './TopNav.css'

// 知识库 slug -> 中文展示名 + 图标 + 主题色（从 App.jsx 挪过来，只有个人信息卡
// 里的"可访问知识库"用得到，搬到顶部导航就不用在 App.jsx 里保留一份了）。
//
// it_kb/attendance_kb/logistics_kb/legal_kb 是老的单文档部门库（迁移遗留，
// 见 role.md 迁移记录），hr_admin_kb 起下面这 6 个才是"全库混合召回"改造后
// 固定下来的部门知识库清单（跟 query_knowledge_hub.py 的 DEPARTMENT_KB_COLLECTIONS
// 一一对应，图标/颜色只是前端展示用，不影响检索）。两组并存，没有互相替代，
// 老的 4 个目前还没清理掉。
const KB_META = {
  default: { label: '通用知识库', icon: BookOpen, color: '#6b7280' },
  it_kb: { label: 'IT 知识库', icon: Cpu, color: '#3b82f6' },
  attendance_kb: { label: '考勤知识库', icon: Clock, color: '#f59e0b' },
  logistics_kb: { label: '后勤知识库', icon: Package, color: '#10b981' },
  legal_kb: { label: '法务知识库', icon: Scale, color: '#ef4444' },
  hr_admin_kb: { label: '人力资源与行政知识库', icon: Users, color: '#8b5cf6' },
  finance_kb: { label: '财务与报销制度知识库', icon: Wallet, color: '#059669' },
  it_support_kb: { label: 'IT 支持与技术运维知识库', icon: Wrench, color: '#2563eb' },
  sales_marketing_kb: { label: '销售话术与市场知识库', icon: Megaphone, color: '#f97316' },
  rd_product_kb: { label: '研发与产品代码知识库', icon: Code2, color: '#6366f1' },
  customer_success_kb: { label: '客户成功与售后服务知识库', icon: HeartHandshake, color: '#ec4899' },
}
const KB_META_FALLBACK = { icon: FilesIcon, color: '#6b7280' }
const KB_META_UNLIMITED = { label: '不限', icon: Sparkle, color: '#8b5cf6' }

// tenant_{name}_kb 是委托模式企业（Acme/Globex 这类）各自独立知识库的
// collection 命名约定（见 scripts/ingest_tenant_kb_corpus.py）；企业自己的
// 知识库服务可以在契约响应里额外报一个 kb_name（人话标签，见
// knowledge-base-tenant-federation.md 第 4.2 节），这时后端会把它拼成
// "tenant_xxx_kb:人力资源" 这样的 collection 标签（见 query_knowledge_hub.py
// _execute_remote）——这里匹配出冒号后面那段子库标签一并展示；没有这个可选
// 字段的企业还是老的一整段 "tenant_xxx_kb"，展示成笼统的"本企业知识库"。
const TENANT_KB_RE = /^tenant_.+_kb(?::(.+))?$/

export function kbMeta(slug) {
  const tenantMatch = TENANT_KB_RE.exec(slug)
  if (tenantMatch) {
    const subLabel = tenantMatch[1]
    return {
      ...KB_META_FALLBACK,
      label: subLabel ? `本企业知识库 · ${subLabel}` : '本企业知识库',
      icon: Building2,
      color: '#0f7d6b',
    }
  }
  return { ...KB_META_FALLBACK, label: slug, ...KB_META[slug] }
}

export function KbTag({ slug }) {
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
          {meProfile?.organization && (
            <div className="profile-org">
              <Building2 size={11} /> {meProfile.organization.name}
            </div>
          )}
          {meProfile && <div className="profile-joined">加入于 {formatDate(meProfile.created_at)}</div>}
        </div>
      </div>

      <div className="profile-section">
        <div className="profile-section-label">角色</div>
        <div className="profile-collections">
          {meProfile?.roles?.length ? (
            meProfile.roles.map((r) => (
              <span key={r.role_id} className="tag tag--primary">{r.display_name}</span>
            ))
          ) : (
            <span className="profile-empty-hint">未分配角色</span>
          )}
        </div>
      </div>

      <div className="profile-section">
        <div className="profile-section-label">可访问知识库</div>
        <div className="profile-collections">
          {(() => {
            // 跟 UserRoleAssignment.jsx 的知识库列用同一套口径：org_admin 对
            // 自己企业的知识库是隐式全权限（不是靠 role_collections 表里挂了
            // 通配符），allowed_collections 字段对它来说本来就是空数组——
            // 这里不特判就会跟"仅自己的对话"（无知识库权限）撞成一个显示，
            // 企业管理员会误以为自己看不了知识库。
            const roleNames = meProfile?.roles?.map((r) => r.name) ?? []
            if (roleNames.includes('super_admin') || roleNames.includes('admin')) {
              return <span className="profile-empty-hint">无（平台管理员不管理知识库）</span>
            }
            if (roleNames.includes('org_admin')) {
              return <span className="tag tag--primary">企业内全部知识库</span>
            }
            if (meProfile?.allowed_collections?.includes('*')) {
              return <KbTag slug="*" />
            }
            if (meProfile?.allowed_collections?.length) {
              return meProfile.allowed_collections.map((c) => <KbTag key={c} slug={c} />)
            }
            return <span className="profile-empty-hint">仅自己的对话</span>
          })()}
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
