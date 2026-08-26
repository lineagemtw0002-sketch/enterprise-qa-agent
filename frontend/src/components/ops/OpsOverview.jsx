import { useCallback, useEffect, useState } from 'react'
import { Button, Popconfirm, Spin, message } from 'antd'
import { RefreshCw } from 'lucide-react'
import * as opsApi from '../../api/ops.js'
import { displayUser, useUserNames } from './opsDisplay.jsx'

// 运维塔台「总览」大屏。视觉照 docs/design_reference/aiops_console_mockup.html，
// **数据全部来自真实接口**——设计稿里那些示例数字（1,284 条告警合并、MTTR 6.4 分钟、
// 5 个服务的健康网格）一个都没有照抄。
//
// ⚠️ **设计稿里有三块东西是刻意不做的，不是漏了**（用户已确认）：
//
// | 设计稿元素 | 为什么不做 |
// |---|---|
// | 「今日告警合并」KPI | 指标定义本身还没有，属于 aiops_module_design.md §9.3 记录的"效果度量未定义"空白 |
// | 「MTTR（中位数）」KPI | 同上；而且 MTTR 需要"故障开始时间"，系统里没有这个概念 |
// | 服务健康网格（svc-grid） | 系统里没有"这家企业有哪些服务"的清单，画出来每一项都是编的 |
//
// 用户的原话是宁可不显示，也不编示例数字顶上。**如果以后有人要把它们加回来，
// 先解决的是数据来源问题，不是前端布局问题。**

const ACTION_TYPE_LABEL = {
  restart_service: '重启服务',
  scale_instances: '扩缩容',
  clean_disk: '清理磁盘',
  rollback_deployment: '回滚版本',
}

const POLL_INTERVAL_MS = 15000

function fmtClock(ts) {
  if (!ts) return '--:--'
  return new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' })
}

function fmtFull(ts) {
  return ts ? new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'
}

function Kpi({ label, value, unit, delta, deltaTone = 'flat' }) {
  return (
    <div className="kpi-card">
      <div className="kpi-label">{label}</div>
      <div className="kpi-row">
        <span className="kpi-value">
          {value}
          {unit && <span className="kpi-unit"> {unit}</span>}
        </span>
        {delta && <span className={`kpi-delta ${deltaTone}`}>{delta}</span>}
      </div>
    </div>
  )
}

/** 分析摘要 → 时间线一行。
 *
 * 严重度是**从依据里推出来的**（有异常检出 = warning，含告警关联事件 = critical），
 * 不是模型给的——依据本身就是代码算出来的（见 src/ops/analysis/rca.py）。
 * 这样时间线上的颜色跟"我们实际观测到什么"一一对应，不会出现"模型说很严重
 * 所以标红"这种没有事实支撑的视觉强调。
 */
function summaryTone(summary) {
  const refs = summary.evidence_refs || []
  const hasIncident = refs.some((r) => r.source === 'alert_correlation')
  const hasAnomaly = refs.some((r) => r.source === 'anomaly_detection' && r.detail?.anomaly_count > 0)
  if (hasIncident) return 'critical'
  if (hasAnomaly) return 'warning'
  return 'good'
}

/** 摘要拆成标题 + 正文。第一行当标题（RCA 的 summary 第一句就是概括），
 *  没有换行时截断一段当标题、正文留空，避免同一句话渲染两遍。 */
function splitSummary(text) {
  const lines = String(text || '').split('\n').filter((l) => l.trim())
  if (!lines.length) return { title: '分析结论', body: '' }
  if (lines.length === 1) {
    const only = lines[0]
    return only.length > 48 ? { title: `${only.slice(0, 48)}…`, body: only } : { title: only, body: '' }
  }
  return { title: lines[0], body: lines.slice(1).join('\n') }
}

function evidenceTags(summary) {
  const tags = []
  for (const ref of summary.evidence_refs || []) {
    if (ref.source === 'alert_correlation' && ref.detail?.alert_count != null) {
      tags.push(`关联告警 ×${ref.detail.alert_count}`)
    }
    if (ref.source === 'anomaly_detection') {
      const d = ref.detail || {}
      if (d.evaluated === false) tags.push(`${d.target || '指标'}：未评估`)
      else if (d.anomaly_count) tags.push(`${d.metric || '指标'} 异常 ×${d.anomaly_count}`)
    }
  }
  return tags
}

/** 比例指标的一格。
 *
 * ⚠️ **`value == null` 显示「暂无数据」，不显示 0%。** 后端在分母为 0 时刻意返回
 * null 而不是 0.0，前端把它渲染成 0% 等于把这份用心扔掉——"刚开始用、还没样本"
 * 会看起来像"表现很差"。
 *
 * 同时把样本量显示出来：1 个样本算出的 100% 和 200 个样本算出的 100% 在决策上
 * 完全是两回事，只给百分比等于把这个差别藏起来。
 */
/** 样本量。后端给的是分状态计数（approved/rejected/expired/completed/failed/
 *  alert_count/incident_count），不是按指标聚合好的数——这里按指标把该算进
 *  分母的那几项加起来。
 *
 *  ⚠️ 这些键名是**照着 ops_store.compute_ops_metrics 的返回值抄的**，不是猜的：
 *  第一版我按语义猜了 `approval_decisions`/`executions`/`correlation_runs`，
 *  四个全错——而这类错不会让页面报错，只会让样本量默默显示成"—"。
 */
function sumSamples(metrics, keys) {
  const sizes = metrics?.sample_sizes
  if (!sizes) return undefined
  return keys.reduce((acc, k) => acc + (sizes[k] || 0), 0)
}

function MetricCell({ label, value, sample, hint }) {
  const hasValue = value !== null && value !== undefined
  return (
    <div className="metric-cell">
      <div className="metric-label">{label}</div>
      <div className={`metric-value ${hasValue ? '' : 'empty'}`}>
        {hasValue ? `${(value * 100).toFixed(1)}%` : '暂无数据'}
      </div>
      <div className="metric-sample">
        {hasValue ? `样本 ${sample ?? '—'}` : (hint || '还没有可统计的样本')}
      </div>
    </div>
  )
}

export default function OpsOverview({ canManage, onModuleDisabled }) {
  const [actions, setActions] = useState([])
  const [metrics, setMetrics] = useState(null)
  const [connectors, setConnectors] = useState([])
  const [summaries, setSummaries] = useState([])
  const [loading, setLoading] = useState(true)
  const [acting, setActing] = useState('')
  // user_id → 用户名，三个视图共用同一份实现（见 opsDisplay.jsx）
  const userNames = useUserNames(canManage)

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true)
    try {
      // 三个接口并发拉。任意一个 403 都说明模块没开通（后端是统一的门），
      // 交给外层显示"未开通"，不在这里各自处理。
      const [actionList, summaryList, connectorList, metricsData] = await Promise.all([
        opsApi.listRemediationActions(),
        opsApi.listAnalysisSummaries(),
        // 非管理员没有连接器列表权限（后端 org_admin 专属），拿不到就当空——
        // 他看到的 KPI 里"连接器在线"那张会显示"—"，不是报错。
        canManage ? opsApi.listConnectors() : Promise.resolve([]),
        // 指标算不出来不该让整块大屏挂掉——它是"锦上添花"的那一层，
        // 失败时显示"暂无数据"就够了。
        opsApi.getOpsMetrics().catch(() => null),
      ])
      setActions(actionList)
      setSummaries(summaryList)
      setConnectors(connectorList)
      setMetrics(metricsData)
    } catch (error) {
      if (opsApi.isModuleDisabledError(error)) { onModuleDisabled(); return }
      if (!silent) message.error(opsApi.errorText(error))
    } finally {
      if (!silent) setLoading(false)
    }
  }, [canManage, onModuleDisabled])

  useEffect(() => {
    load()
    const timer = setInterval(() => load(true), POLL_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [load])

  async function act(actionId, kind) {
    setActing(actionId)
    try {
      if (kind === 'approve') await opsApi.approveRemediationAction(actionId)
      else await opsApi.rejectRemediationAction(actionId)
      message.success(kind === 'approve' ? '已批准' : '已拒绝')
      load()
    } catch (error) {
      message.warning(opsApi.errorText(error))
      load()
    } finally {
      setActing('')
    }
  }

  const pending = actions.filter((a) => a.status === 'pending_approval')
  const executing = actions.filter((a) => a.status === 'executing')
  const online = connectors.filter((c) => c.connector_status === 'online').length

  if (loading) return <div className="panel-empty"><Spin /></div>

  return (
    <>
      <section className="kpi-strip" aria-label="核心指标">
        <Kpi
          label="待审批修复"
          value={pending.length}
          delta={pending.length ? '需要人工确认' : '暂无待办'}
          deltaTone={pending.length ? 'bad' : 'flat'}
        />
        <Kpi
          label="连接器在线"
          value={canManage ? `${online}/${connectors.length}` : '—'}
          delta={!canManage ? '需要管理员权限' : (connectors.length && online === connectors.length ? '全部在线' : '有连接器离线')}
          deltaTone={!canManage ? 'flat' : (connectors.length && online === connectors.length ? 'good' : 'bad')}
        />
        <Kpi
          label="进行中事件"
          value={executing.length}
          delta={executing.length ? '正在客户环境执行' : '无执行中动作'}
          deltaTone={executing.length ? 'bad' : 'flat'}
        />
      </section>

      <div className="ops-grid">
        <div className="ops-col">
          <section className="panel">
            <div className="panel-head">
              <div className="panel-title">
                告警关联时间线 <span className="count">{summaries.length ? `最近 ${summaries.length} 次分析` : ''}</span>
              </div>
              <button className="ops-tabbtn" onClick={() => load()} title="刷新">
                <RefreshCw size={13} />
              </button>
            </div>
            <div className="panel-body">
              {summaries.length === 0 ? (
                <>
                  <div className="panel-empty">还没有任何分析记录</div>
                  {/* 空态要说清楚"为什么空"，否则会被当成功能坏了 */}
                  <p className="ops-note">
                    这里显示的是智能运维实际跑过的分析（异常检测 → 告警关联 → 根因辅助）。
                    在对话里让它分析某个服务之后，结果会出现在这里。
                  </p>
                </>
              ) : (
                <div className="timeline">
                  {summaries.map((s, idx) => (
                    <div className="tl-row" key={s.summary_id}>
                      <div className="tl-time">{fmtClock(s.created_at)}</div>
                      <div className="tl-marker">
                        <div className={`tl-dot ${summaryTone(s)}`} />
                        {idx < summaries.length - 1 && <div className="tl-line" />}
                      </div>
                      <div>
                        {/* 摘要第一行当标题、其余当正文——两处都渲染整段的话，
                            第一句会在同一个卡片里出现两次。 */}
                        <p className="tl-title">{splitSummary(s.summary).title}</p>
                        {splitSummary(s.summary).body && (
                          <p className="tl-desc">{splitSummary(s.summary).body}</p>
                        )}
                        <div className="tl-tag">
                          {evidenceTags(s).map((t) => <span key={t}>{t}</span>)}
                        </div>
                      </div>
                      <div className="tl-count">{(s.evidence_refs || []).length} 条依据</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </section>
          <section className="panel">
            <div className="panel-head">
              <div className="panel-title">V1 效果指标</div>
              <div className="panel-note">按 §10.5 定义真实计算，不预设目标值</div>
            </div>
            <div className="panel-body">
              <div className="metric-grid">
                <MetricCell
                  label="审批处理及时率"
                  value={metrics?.approval_timeliness_rate}
                  sample={sumSamples(metrics, ['approved', 'rejected', 'expired'])}
                  hint="还没有审批记录"
                />
                <MetricCell
                  label="执行成功率"
                  value={metrics?.execution_success_rate}
                  sample={sumSamples(metrics, ['completed', 'failed'])}
                  hint="还没有执行完成的动作"
                />
                <MetricCell
                  label="告警合并率"
                  value={metrics?.alert_noise_reduction}
                  sample={metrics?.sample_sizes?.alert_count}
                  hint="还没有跑过告警关联"
                />
                <div className="metric-cell">
                  <div className="metric-label">事后有效性</div>
                  {/* 刻意不折成一个比例：设计文档没给公式，而且"未标注"的数量
                      本身就是有意义的信息（标注覆盖率）。折成百分比会把
                      "大部分没人标"和"标了但效果不好"糊成同一个数字。 */}
                  <div className="metric-value small">
                    <span className="good">{metrics?.outcome_effective_counts?.effective ?? 0} 有效</span>
                    {' / '}
                    <span className="bad">{metrics?.outcome_effective_counts?.ineffective ?? 0} 无效</span>
                  </div>
                  <div className="metric-sample">
                    {metrics?.outcome_effective_counts?.unlabeled ?? 0} 条未标注
                  </div>
                </div>
              </div>
              <p className="ops-note">
                这四个指标的目的是<b>能测量</b>，不是达到某个数字。执行成功率和事后有效性
                在没有真实连接器进程时会长期是空的——那是诚实的空，不是统计出错。
              </p>
            </div>
          </section>
        </div>

        <div className="ops-col">
          <section className="panel">
            <div className="panel-head">
              <div className="panel-title">待审批修复 <span className="count">{pending.length} 项</span></div>
            </div>
            <div className="panel-body">
              {pending.length === 0 ? (
                <div className="panel-empty">没有待审批的修复动作</div>
              ) : pending.map((a) => (
                <div className="approval-card" key={a.action_id}>
                  <div className="ap-head">
                    <span className="ap-type">{ACTION_TYPE_LABEL[a.plan?.action_type] || '修复动作'}</span>
                    <span className="status-pill warning"><span className="dot" />待审批</span>
                  </div>
                  <p className="ap-intent">{a.intent}</p>
                  <div className="ap-meta">
                    {a.plan?.target && <span>目标 <b>{a.plan.target}</b></span>}
                    <span>提议 <b>{displayUser(userNames, a.proposed_by)}</b></span>
                    <span>时间 <b>{fmtFull(a.created_at)}</b></span>
                  </div>
                  {a.impact_radius && (
                    <div className="ap-radius">
                      <span>
                        影响半径「{a.impact_radius}」是 AI 的定性推断，不是实时拓扑，实际范围可能更大或更小；
                        真正的可执行边界以「白名单配置」里登记的允许范围为准。
                      </span>
                    </div>
                  )}
                  <div className="ap-actions">
                    <Popconfirm
                      title="确认批准这个修复动作？"
                      description="批准 = 授予执行资格，本身不会立刻下发；真正执行由智能运维在对话里另行发起。"
                      okText="确认批准" cancelText="再想想"
                      onConfirm={() => act(a.action_id, 'approve')}
                    >
                      <Button size="small" type="primary" loading={acting === a.action_id}>批准</Button>
                    </Popconfirm>
                    <Button size="small" danger onClick={() => act(a.action_id, 'reject')}>拒绝</Button>
                  </div>
                </div>
              ))}
            </div>
          </section>

          <section className="panel">
            <div className="panel-head">
              <div className="panel-title">连接器状态 <span className="count">{canManage ? `${online}/${connectors.length} 在线` : ''}</span></div>
            </div>
            <div className="panel-body">
              {!canManage ? (
                <div className="panel-empty">查看连接器需要企业管理员权限</div>
              ) : connectors.length === 0 ? (
                <div className="panel-empty">还没有登记任何连接器</div>
              ) : connectors.map((c) => (
                <div className="conn-row" key={c.connection_id}>
                  <div>
                    <div className="conn-name">{c.name}</div>
                    <div className="conn-meta">
                      {c.system_type} · 最近心跳 {c.last_heartbeat_at ? fmtFull(c.last_heartbeat_at) : '从未'}
                    </div>
                  </div>
                  <span className={`status-pill ${c.connector_status === 'online' ? 'good' : 'pending'}`}>
                    <span className="dot" />{c.connector_status === 'online' ? '在线' : '离线'}
                  </span>
                </div>
              ))}
            </div>
          </section>
        </div>
      </div>
    </>
  )
}
