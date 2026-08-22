import { useEffect, useState } from 'react'
import { Segmented, Card, Statistic, Spin, message, Tooltip } from 'antd'
import { Coins, DollarSign, CheckCircle2, XCircle, RefreshCw, TrendingUp, TrendingDown } from 'lucide-react'
import * as adminApi from '../../api/admin.js'
import SimpleTrendChart from './SimpleTrendChart.jsx'

const WINDOW_OPTIONS = [
  { label: '近 24 小时', value: '24h' },
  { label: '近 7 天', value: '7d' },
  { label: '近 30 天', value: '30d' },
]

function ChangeBadge({ value, invertColor = false }) {
  if (value === null || value === undefined) return <span className="kpi-change kpi-change--flat">环比 —</span>
  const isUp = value > 0
  const isFlat = value === 0
  // invertColor：失败率这类"涨了反而不好"的指标，颜色语义要反过来
  const good = invertColor ? !isUp : isUp
  const Icon = isUp ? TrendingUp : TrendingDown
  return (
    <span className={`kpi-change ${isFlat ? 'kpi-change--flat' : good ? 'kpi-change--up' : 'kpi-change--down'}`}>
      {!isFlat && <Icon size={12} />} 环比 {isUp ? '+' : ''}{value}%
    </span>
  )
}

// 「成本与质量」——运营仪表盘新增的子页面。Token 用量/预估成本来自
// conversation_archive 里每轮 generate 节点记的用量（真实 usage_metadata 优先，
// 本地 Ollama 模型没有回传时按字符数估算，后端会标注 estimated），工具调用
// 成功率来自审计日志（audit_logs，治理与合规那组端点/回调顺手落的记录）——
// 两类数据都是各自需求本来就要落库的，这里只是复用展示，不是为了这个页面
// 单独造的埋点。跟"概览"子页一样只对平台管理员开放。
export default function CostQualityDashboard() {
  const [window_, setWindow] = useState('7d')
  const [overview, setOverview] = useState(null)
  const [overviewLoading, setOverviewLoading] = useState(false)
  const [trends, setTrends] = useState({})
  const [trendsLoading, setTrendsLoading] = useState(false)

  async function loadOverview() {
    setOverviewLoading(true)
    try {
      setOverview(await adminApi.getCostOverview(window_))
    } catch (error) {
      message.error('加载成本概览失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setOverviewLoading(false)
    }
  }

  async function loadTrends() {
    setTrendsLoading(true)
    try {
      const [tokens, successRate] = await Promise.all([
        adminApi.getCostTrend('tokens', window_),
        adminApi.getCostTrend('tool_success_rate', window_),
      ])
      setTrends({ tokens: tokens.points, tool_success_rate: successRate.points })
    } catch (error) {
      message.error('加载趋势图失败: ' + (error.response?.data?.detail || error.message))
    } finally {
      setTrendsLoading(false)
    }
  }

  useEffect(() => {
    loadOverview()
    loadTrends()
  }, [window_])

  function costDisplay() {
    if (overviewLoading) return <Spin size="small" />
    if (overview?.estimated_cost_usd === null || overview?.estimated_cost_usd === undefined) {
      return <div className="kpi-change kpi-change--flat" style={{ marginTop: 6 }}>当前模型无可靠单价参考</div>
    }
    if (overview.estimated_cost_usd === 0) {
      return <Statistic value={0} prefix="$" precision={2} />
    }
    return <Statistic value={overview.estimated_cost_usd} prefix="$" precision={4} />
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', margin: '0 0 16px' }}>
        <p style={{ margin: 0, color: 'var(--text-tertiary, #888)' }}>
          Token 用量/预估成本来自每轮回答的模型用量（本地模型无真实计费时按字符数估算，会标注"预估"）；
          工具调用成功率来自审计日志。跟「概览」一样只是平台整体数据，不涉及任何企业的知识库内容。
        </p>
        <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexShrink: 0 }}>
          <Segmented options={WINDOW_OPTIONS} value={window_} onChange={setWindow} />
          <RefreshCw
            size={16}
            style={{ cursor: 'pointer', color: 'var(--text-tertiary, #888)' }}
            onClick={() => { loadOverview(); loadTrends() }}
          />
        </div>
      </div>

      <div className="kpi-grid">
        <Card size="small" className="kpi-card">
          <div className="kpi-card-header"><Coins size={16} color="#f59e0b" /><span>总 Token 用量</span></div>
          {overviewLoading ? <Spin size="small" /> : (
            <>
              <Statistic value={overview?.total_tokens ?? 0} />
              <ChangeBadge value={overview?.total_tokens_change} />
            </>
          )}
        </Card>

        <Card size="small" className="kpi-card">
          <div className="kpi-card-header">
            <DollarSign size={16} color="#059669" />
            <span>预估成本</span>
            <Tooltip title="基于配置模型的公开参考单价估算，不代表实时/合同价格；本地模型无推理费用">
              <span style={{ cursor: 'help', color: 'var(--text-tertiary, #999)' }}>?</span>
            </Tooltip>
          </div>
          {costDisplay()}
        </Card>

        <Card size="small" className="kpi-card">
          <div className="kpi-card-header"><CheckCircle2 size={16} color="#2563eb" /><span>工具调用成功率</span></div>
          {overviewLoading ? <Spin size="small" /> : overview?.tool_success_rate === null || overview?.tool_success_rate === undefined ? (
            <div className="kpi-change kpi-change--flat">暂无工具调用</div>
          ) : (
            <>
              <Statistic value={overview.tool_success_rate} suffix="%" precision={1} />
              <ChangeBadge value={overview.tool_success_rate_change} />
            </>
          )}
        </Card>

        <Card size="small" className="kpi-card">
          <div className="kpi-card-header"><XCircle size={16} color="#dc2626" /><span>工具调用失败次数</span></div>
          {overviewLoading ? <Spin size="small" /> : <Statistic value={overview?.tool_failure_count ?? 0} />}
        </Card>
      </div>

      <div className="trend-grid">
        <Card size="small" title="Token 用量趋势" className="trend-card">
          {trendsLoading && !trends.tokens ? (
            <div className="trend-chart-empty"><Spin size="small" /></div>
          ) : (
            <SimpleTrendChart points={trends.tokens} window={window_} color="#f59e0b" loading={trendsLoading} />
          )}
        </Card>
        <Card size="small" title="工具调用成功率趋势" className="trend-card">
          {trendsLoading && !trends.tool_success_rate ? (
            <div className="trend-chart-empty"><Spin size="small" /></div>
          ) : (
            <SimpleTrendChart
              points={trends.tool_success_rate}
              window={window_}
              color="#2563eb"
              loading={trendsLoading}
              valueFormatter={(v) => `${Math.round(v)}%`}
            />
          )}
        </Card>
      </div>
    </div>
  )
}
