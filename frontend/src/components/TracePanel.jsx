import { useMemo, useState } from 'react'
import { Activity, Check, X, ChevronDown, Monitor } from 'lucide-react'
import './TracePanel.css'

const STEP_LABELS = {
  session: '会话初始化',
  intent: '意图解析',
  clarify: '澄清提示',
  retrieve: '知识库检索',
  tool_subgraph: '工具调用',
  generate: '最终生成',
  memory_manage: '记忆管理',
  archive: '归档存储',
}

const NODES = ['session', 'intent', 'clarify', 'retrieve', 'tool_subgraph', 'generate', 'memory_manage', 'archive']

function previewPayload(payload) {
  const limited = {}
  const keys = Object.keys(payload).slice(0, 4)
  for (const k of keys) limited[k] = payload[k]
  return limited
}

function formatValue(val) {
  if (typeof val === 'boolean') return val ? 'true' : 'false'
  if (typeof val === 'number') return String(val)
  if (Array.isArray(val)) return `[${val.length} items]`
  if (typeof val === 'object' && val !== null) return JSON.stringify(val).slice(0, 40)
  return String(val).slice(0, 60)
}

function syntaxHighlight(json) {
  if (!json) return ''
  return json
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/(".*?")/g, '<span class="json-key">$1</span>')
    .replace(/\b(true|false|null)\b/g, '<span class="json-bool">$1</span>')
    .replace(/\b(\d+(?:\.\d+)?)\b/g, '<span class="json-number">$1</span>')
}

export default function TracePanel({ traces = [] }) {
  const [inspectorExpanded, setInspectorExpanded] = useState(true)

  const displaySteps = useMemo(() => {
    const result = []
    for (const node of NODES) {
      const nodeTraces = traces.filter((t) => t.node === node)
      if (nodeTraces.length === 0) {
        result.push({ id: node + '_pending', node, label: STEP_LABELS[node] || node, status: 'pending', payload: {}, duration: null })
        continue
      }

      const nodeEnd = nodeTraces.find((t) => t.step === 'node_end')
      const error = nodeTraces.find((t) => t.status === 'error')
      const running = nodeTraces.find((t) => t.status === 'running')
      const success = nodeTraces.filter((t) => t.status === 'success')

      let status = 'pending'
      let payload = {}
      let duration = null

      if (error) {
        status = 'error'
        payload = error.payload || {}
      } else if (nodeEnd && nodeEnd.status === 'success') {
        status = 'success'
        payload = success.length > 0 ? success[success.length - 1].payload || {} : nodeEnd.payload || {}
        const starts = nodeTraces.filter((t) => t.status === 'running').map((t) => t.ts)
        const ends = success.map((t) => t.ts)
        if (starts.length && ends.length) {
          duration = Math.round((Math.max(...ends) - Math.min(...starts)) * 1000)
        }
      } else if (running) {
        status = 'running'
        payload = running.payload || {}
      }

      result.push({ id: node + '_' + status, node, label: STEP_LABELS[node] || node, status, payload, duration })
    }
    return result
  }, [traces])

  const runningCount = displaySteps.filter((s) => s.status === 'running').length
  const hasTraces = traces.length > 0

  const latestState = useMemo(() => {
    if (traces.length === 0) return {}
    const last = traces[traces.length - 1]
    return { node: last.node, step: last.step, status: last.status, ts: last.ts, payload: last.payload }
  }, [traces])

  const highlightedState = useMemo(() => syntaxHighlight(JSON.stringify(latestState, null, 2)), [latestState])

  return (
    <div className="trace-panel">
      <div className="trace-header">
        <div className="trace-title">
          <Activity size={18} />
          <span>LANGGRAPH 实时追踪</span>
        </div>
        {runningCount > 0 ? (
          <span className="running-tag running-tag--primary">RUNNING {runningCount}</span>
        ) : hasTraces ? (
          <span className="running-tag running-tag--success">COMPLETE</span>
        ) : null}
      </div>

      <div className="trace-timeline">
        {displaySteps.map((item, index) => (
          <div key={item.id} className={`trace-step ${item.status}`}>
            <div className="step-indicator">
              {item.status === 'running' && <div className="step-pulse" />}
              <div className="step-dot">
                {item.status === 'success' && <Check size={14} color="#10b981" />}
                {item.status === 'error' && <X size={14} color="#ef4444" />}
                {item.status === 'running' && <span className="dot-spinner" />}
                {item.status !== 'success' && item.status !== 'error' && item.status !== 'running' && (
                  <span className="dot-idle" />
                )}
              </div>
              {index < displaySteps.length - 1 && <div className={`step-line ${item.status}`} />}
            </div>
            <div className="step-content">
              <div className="step-name">{item.label}</div>
              <div className="step-meta">
                {item.status === 'running' && <span className="meta-running">执行中</span>}
                {item.status === 'success' && <span className="meta-success">已完成</span>}
                {item.status === 'error' && <span className="meta-error">失败</span>}
                {item.status === 'pending' && <span className="meta-pending">等待中</span>}
                {item.duration && <span className="meta-duration">{item.duration}ms</span>}
              </div>
              {item.payload && Object.keys(item.payload).length > 0 && (
                <div className="step-payload">
                  {Object.entries(previewPayload(item.payload)).map(([key, val]) => (
                    <div key={key} className="payload-item">
                      <span className="payload-key">{key}:</span>
                      <span className="payload-val">{formatValue(val)}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>

      <div className="inspector-section">
        <div className="inspector-header" onClick={() => setInspectorExpanded((v) => !v)}>
          <Monitor size={14} />
          <span>STATE INSPECTOR</span>
          <ChevronDown size={14} className={`expand-icon ${inspectorExpanded ? 'expanded' : ''}`} />
        </div>
        {inspectorExpanded && (
          <div className="inspector-body">
            <pre className="inspector-code">
              <code dangerouslySetInnerHTML={{ __html: highlightedState }} />
            </pre>
          </div>
        )}
      </div>
    </div>
  )
}
