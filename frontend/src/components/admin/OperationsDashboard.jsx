import { useEffect, useState } from 'react'
import { Segmented, Card, Statistic, Spin, Menu, message, Empty } from 'antd'
import {
  MessageSquare, Users, Clock, TrendingUp, TrendingDown, RefreshCw,
  LayoutDashboard, ListChecks, Building2, Plug, Coins, FlaskConical,
} from 'lucide-react'
import * as adminApi from '../../api/admin.js'
import SimpleTrendChart from './SimpleTrendChart.jsx'
import WorkflowTemplateManagement from './WorkflowTemplateManagement.jsx'
import OrganizationManagement from './OrganizationManagement.jsx'
import GatewayMonitor from './GatewayMonitor.jsx'
import CostQualityDashboard from './CostQualityDashboard.jsx'
import KnowledgeBaseTestQuery from './KnowledgeBaseTestQuery.jsx'  // 【测试专用，正式上线前删除】
import './OperationsDashboard.css'

const WINDOW_OPTIONS = [
  { label: '近 24 小时', value: '24h' },
  { label: '近 7 天', value: '7d' },
  { label: '近 30 天', value: '30d' },
]

// 「运营仪表盘」现在是四个平台级页面共用的壳：概览（KPI/趋势，本文件原有
// 内容）+ 工作流管理/组织管理/网关监控（原本挂在「权限系统」的 Tabs 里，
// 挪过来跟概览用同一套菜单切换——这四个都是只对平台管理员开放的运维视图，
// 放在一起比分散在两个顶层模块下更符合"运营仪表盘"这个名字）。
const SECTIONS = [
  { key: 'overview', label: '概览', icon: LayoutDashboard },
  { key: 'cost', label: '成本与质量', icon: Coins },
  { key: 'workflows', label: '工作流管理', icon: ListChecks },
  { key: 'organizations', label: '组织管理', icon: Building2 },
  { key: 'gateway', label: '网关', icon: Plug },
  // 【测试专用，正式上线前删除】见 KnowledgeBaseTestQuery.jsx 顶部说明
  { key: 'kb-test', label: '知识库测试（临时）', icon: FlaskConical },
]
const SECTION_MENU_ITEMS = SECTIONS.map((s) => ({
  key: s.key,
  label: s.label,
  icon: <s.icon size={14} />,
}))

const METRICS = [
  { key: 'sessions', title: '会话数', icon: MessageSquare, color: '#2563eb', overviewField: 'session_count', changeField: 'session_count_change' },
  { key: 'messages', title: '消息数', icon: MessageSquare, color: '#059669', overviewField: 'message_count', changeField: 'message_count_change' },
  { key: 'active_users', title: '活跃用户', icon: Users, color: '#f97316', overviewField: 'active_users', changeField: 'active_users_change' },
  { key: 'latency', title: '平均响应耗时', icon: Clock, color: '#8b5cf6', overviewField: 'avg_latency_ms', changeField: 'avg_latency_ms_change', suffix: 'ms' },
]

function ChangeBadge({ value }) {
  if (value === null || value === undefined) return <span className="kpi-change kpi-change--flat">环比 —</span>
  const isUp = value > 0
  const isFlat = value === 0
  const Icon = isUp ? TrendingUp : TrendingDown
  return (
    <span className={`kpi-change ${isFlat ? 'kpi-change--flat' : isUp ? 'kpi-change--up' : 'kpi-change--down'}`}>
      {!isFlat && <Icon size={12} />} 环比 {isUp ? '+' : ''}{value}%
    </span>
  )
}

// 平台管理员的「运营仪表盘」——只做四个有真实数据支撑的指标（会话数/消息数/
// 活跃用户/平均响应耗时），数据来自 conversations/conversation_archive 两张
// 已有表按时间聚合（见后端 dashboard_stats.py），不需要新的埋点。没有做"质量"
// 类指标——系统里目前没有用户反馈（点赞/点踩）机制，编一个不可靠的质量分
// 出来比不做更容易误导人，等有真实信号了再加。
export default function OperationsDashboard() {
  const [section, setSection] = useState('overview')
  const [window_, setWindow] = useState('7d')
  const [overview, setOverview] = useState(null)
  const [overviewLoading, setOverviewLoading] = useState(false)
  const [trends, setTrends] = useState({})
  const [trendsLoading, setTrendsLoading] = useState(false)

  async function loadOverview() {
    setOverviewLoading(true)
    try {
      const data = await adminApi.getDashboardOverview(window_)
      setOverview(data)
    } catch (error) {
      message.error('加载概览失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setOverviewLoading(false)
    }
  }

  async function loadTrends() {
    setTrendsLoading(true)
    try {
      const results = await Promise.all(
        METRICS.map((m) => adminApi.getDashboardTrend(m.key, window_)),
      )
      const next = {}
      METRICS.forEach((m, i) => { next[m.key] = results[i].points })
      setTrends(next)
    } catch (error) {
      message.error('加载趋势图失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setTrendsLoading(false)
    }
  }

  // 只在「概览」这个子页面激活时才拉概览/趋势接口——切到工作流管理/组织
  // 管理/网关这几个子页面时没必要一直轮询这两个接口。
  useEffect(() => {
    if (section !== 'overview') return
    loadOverview()
    loadTrends()
  }, [window_, section])

  return (
    <div className="dashboard-view">
      <h2 className="module-title">运营仪表盘</h2>

      <Menu
        mode="horizontal"
        selectedKeys={[section]}
        items={SECTION_MENU_ITEMS}
        onClick={({ key }) => setSection(key)}
        className="dashboard-section-menu"
      />

      {section === 'overview' && (
        <>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', margin: '20px 0 16px' }}>
            <p style={{ margin: 0, color: 'var(--text-tertiary, #888)' }}>
              平台整体运营指标，不涉及任何企业的知识库内容。
            </p>
            <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
              <Segmented options={WINDOW_OPTIONS} value={window_} onChange={setWindow} />
              <RefreshCw
                size={16}
                style={{ cursor: 'pointer', color: 'var(--text-tertiary, #888)' }}
                onClick={() => { loadOverview(); loadTrends() }}
              />
            </div>
          </div>

          <div className="kpi-grid">
            {METRICS.map((m) => {
              const Icon = m.icon
              const value = overview?.[m.overviewField]
              const change = overview?.[m.changeField]
              return (
                <Card key={m.key} size="small" className="kpi-card">
                  <div className="kpi-card-header">
                    <Icon size={16} color={m.color} />
                    <span>{m.title}</span>
                  </div>
                  {overviewLoading ? (
                    <Spin size="small" />
                  ) : (
                    <>
                      <Statistic value={value ?? 0} suffix={m.suffix} precision={m.suffix ? 1 : 0} />
                      <ChangeBadge value={change} />
                    </>
                  )}
                </Card>
              )
            })}
          </div>

          <div className="trend-grid">
            {METRICS.map((m) => (
              <Card key={m.key} size="small" title={`${m.title}趋势`} className="trend-card">
                {trendsLoading && !trends[m.key] ? (
                  <div className="trend-chart-empty"><Spin size="small" /></div>
                ) : (
                  <SimpleTrendChart
                    points={trends[m.key]}
                    window={window_}
                    color={m.color}
                    loading={trendsLoading}
                    valueFormatter={m.suffix ? (v) => `${Math.round(v)}${m.suffix}` : undefined}
                  />
                )}
              </Card>
            ))}
          </div>
        </>
      )}

      {section === 'cost' && (
        <div className="dashboard-section-body">
          <CostQualityDashboard />
        </div>
      )}

      {section === 'workflows' && (
        <div className="dashboard-section-body">
          <WorkflowTemplateManagement />
        </div>
      )}

      {section === 'organizations' && (
        <div className="dashboard-section-body">
          <OrganizationManagement />
        </div>
      )}

      {section === 'gateway' && (
        <div className="dashboard-section-body">
          <GatewayMonitor />
        </div>
      )}

      {/* 【测试专用，正式上线前删除】 */}
      {section === 'kb-test' && (
        <div className="dashboard-section-body">
          <KnowledgeBaseTestQuery />
        </div>
      )}
    </div>
  )
}
