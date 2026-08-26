import { useCallback, useEffect, useState } from 'react'
import { Button, Empty, Space, Spin, Tag, message } from 'antd'
import { RefreshCw } from 'lucide-react'
import * as opsApi from '../../api/ops.js'

// 事后复盘（`docs/aiops_module_design.md` §9.2 最小可行版）。
//
// 回答一个问题：**这些自动修复到底有没有用。** 所以一条记录上要同时看得见三样
// 东西——做了什么、结果如何、以及 AI 当初凭什么建议这么做。缺任何一样都没法复盘：
// 只有动作和结果，看不出判断错在哪；只有分析，看不出照做之后发生了什么。
//
// ⚠️ **「无关联分析」和「分析摘要为空」必须分开表达。** 后端的 linked_summary 是
// string | null，null 表示这条动作是人工直接提议的、压根没跑过分析。把它显示成
// "暂无"会让人以为分析跑过但没结论——而实际是根本没分析就动手了，这在复盘时是
// 完全不同的一条线索（前者要查分析质量，后者要问为什么绕过分析）。

const STATUS_META = {
  completed: { color: 'green', label: '执行成功' },
  failed: { color: 'red', label: '执行失败' },
}

const ACTION_TYPE_LABEL = {
  restart_service: '重启服务',
  scale_instances: '扩缩容',
  clean_disk: '清理磁盘',
  rollback_deployment: '回滚版本',
}

function fmt(ts) {
  return ts ? new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'
}

function OutcomeControl({ action, marking, onMark }) {
  if (action.outcome_effective === true) return <Tag color="green">已标注：确实解决了问题</Tag>
  if (action.outcome_effective === false) return <Tag color="red">已标注：没有解决问题</Tag>
  return (
    <Space size={4}>
      <span className="pm-ask">这次修复解决问题了吗？</span>
      <Button size="small" loading={marking === action.action_id}
              onClick={() => onMark(action.action_id, true)}>解决了</Button>
      <Button size="small" danger loading={marking === action.action_id}
              onClick={() => onMark(action.action_id, false)}>没解决</Button>
    </Space>
  )
}

export default function OpsPostmortems({ onModuleDisabled }) {
  const [entries, setEntries] = useState([])
  const [loading, setLoading] = useState(true)
  const [marking, setMarking] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setEntries(await opsApi.listPostmortems())
    } catch (error) {
      if (opsApi.isModuleDisabledError(error)) { onModuleDisabled(); return }
      message.error(opsApi.errorText(error))
    } finally {
      setLoading(false)
    }
  }, [onModuleDisabled])

  useEffect(() => { load() }, [load])

  async function mark(actionId, effective) {
    setMarking(actionId)
    try {
      await opsApi.setActionOutcome(actionId, effective)
      message.success(effective ? '已标注为解决了问题' : '已标注为没有解决问题')
      load()
    } catch (error) {
      message.error(opsApi.errorText(error))
    } finally {
      setMarking('')
    }
  }

  if (loading) return <div className="panel-empty"><Spin /></div>

  const unmarked = entries.filter((e) => e.action.outcome_effective === null
    || e.action.outcome_effective === undefined).length

  return (
    <section className="panel">
      <div className="panel-head">
        <div className="panel-title">
          事后复盘 <span className="count">{entries.length} 条已结束的修复{unmarked ? ` · ${unmarked} 条待标注` : ''}</span>
        </div>
        <button className="ops-tabbtn" onClick={load} title="刷新"><RefreshCw size={13} /></button>
      </div>
      <div className="panel-body">
        {entries.length === 0 ? (
          <>
            <div className="panel-empty"><Empty description="还没有执行完成的修复动作" /></div>
            <p className="ops-note">
              这里只列已经跑完的动作（成功或失败）。待审批、已批准但还没执行的不在这里——
              它们还没有"结果"可以复盘。
            </p>
          </>
        ) : entries.map(({ action, linked_summary: linked }) => {
          const meta = STATUS_META[action.status] || { color: 'default', label: action.status }
          return (
            <div className="pm-card" key={action.action_id}>
              <div className="pm-head">
                <Space size={6}>
                  <span className="ap-type">{ACTION_TYPE_LABEL[action.plan?.action_type] || '修复动作'}</span>
                  <Tag color={meta.color}>{meta.label}</Tag>
                </Space>
                <span className="pm-time">{fmt(action.executed_at || action.created_at)}</span>
              </div>

              <p className="ap-intent">{action.intent}</p>
              <div className="ap-meta">
                {action.plan?.target && <span>目标 <b>{action.plan.target}</b></span>}
                <span>审批人 <b>{action.approver_user_id ? `${String(action.approver_user_id).slice(0, 8)}…` : '—'}</b></span>
              </div>

              {/* 执行结果：失败时最该看的就是这里，不要折叠 */}
              {action.result && (
                <div className="pm-result">
                  <div className="pm-section-label">执行结果</div>
                  <code>{action.result.detail || JSON.stringify(action.result)}</code>
                </div>
              )}

              <div className="pm-analysis">
                <div className="pm-section-label">当初的分析依据</div>
                {linked === null || linked === undefined ? (
                  // 措辞刻意写成"没有经过分析"而不是"暂无"——见文件顶部说明
                  <p className="pm-none">
                    这条动作<b>没有关联任何 AI 分析</b>，是直接提议的。复盘时要问的是
                    「当时为什么绕过分析就动手」，而不是「分析为什么没给出结论」。
                  </p>
                ) : linked.trim() === '' ? (
                  <p className="pm-none">关联了一次分析，但那次分析没有产出任何结论文本。</p>
                ) : (
                  <p className="pm-summary">{linked}</p>
                )}
              </div>

              <div className="pm-outcome">
                <OutcomeControl action={action} marking={marking} onMark={mark} />
              </div>
            </div>
          )
        })}
      </div>
    </section>
  )
}
